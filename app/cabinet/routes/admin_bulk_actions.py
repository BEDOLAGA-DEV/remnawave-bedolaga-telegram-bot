"""Admin routes for bulk actions on users."""

import json
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import (
    add_subscription_traffic,
    create_paid_subscription,
    extend_subscription,
    get_subscription_by_id,
    reactivate_subscription,
)
from app.database.crud.tariff import get_tariff_by_id
from app.database.crud.user import add_user_balance, get_user_by_id
from app.database.crud.user_promo_group import sync_user_primary_promo_group
from app.database.models import (
    PaymentMethod,
    PromoGroup,
    Subscription,
    SubscriptionServer,
    SubscriptionStatus,
    Tariff,
    TrafficPurchase,
    TransactionType,
    User,
    UserPromoGroup,
)

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.bulk_actions import (
    BulkActionParams,
    BulkActionType,
    BulkExecuteRequest,
    BulkExecuteResponse,
    BulkSubscriptionInfo,
    BulkUserResult,
)
from .admin_users import _sync_subscription_to_panel


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/bulk', tags=['Cabinet Admin Bulk Actions'])


# ---------------------------------------------------------------------------
# Param validation helpers
# ---------------------------------------------------------------------------


def _require_days(params: BulkActionParams) -> int:
    if not params.days or params.days <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='params.days must be a positive integer for this action',
        )
    return params.days


def _require_tariff_id(params: BulkActionParams) -> int:
    if params.tariff_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='params.tariff_id is required for change_tariff action',
        )
    return params.tariff_id


def _require_traffic_gb(params: BulkActionParams) -> int:
    if not params.traffic_gb or params.traffic_gb <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='params.traffic_gb must be a positive integer for add_traffic action',
        )
    return params.traffic_gb


def _require_amount_kopeks(params: BulkActionParams) -> int:
    if not params.amount_kopeks or params.amount_kopeks <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='params.amount_kopeks must be a positive integer for add_balance action',
        )
    return params.amount_kopeks


def _require_device_limit(params: BulkActionParams) -> int:
    if not params.device_limit or params.device_limit <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='params.device_limit must be a positive integer for set_devices action',
        )
    return params.device_limit


# ---------------------------------------------------------------------------
# Subscription resolver
# ---------------------------------------------------------------------------


def _resolve_subscription(user: User, override: Subscription | None = None) -> Subscription | None:
    """Return the first active subscription or most recent one (multi/single tariff).

    If *override* is provided, return it directly (used when operating on a
    specific subscription_id rather than auto-resolving).
    """
    if override is not None:
        return override
    subs = getattr(user, 'subscriptions', None) or []
    return next((s for s in subs if s.is_active), subs[0] if subs else None)


# ---------------------------------------------------------------------------
# Per-user action handlers
# ---------------------------------------------------------------------------


async def _do_extend_subscription(
    db: AsyncSession,
    user: User,
    params: BulkActionParams,
    dry_run: bool,
    sub_override: Subscription | None = None,
) -> BulkUserResult:
    days = params.days  # already validated
    sub = _resolve_subscription(user, sub_override)
    if not sub:
        return BulkUserResult(user_id=user.id, success=False, message='No subscription found', username=user.username)

    if dry_run:
        return BulkUserResult(
            user_id=user.id,
            success=True,
            message=f'Would extend subscription by {days} days',
            username=user.username,
        )

    await extend_subscription(db, sub, days)
    await db.refresh(sub)
    await _sync_subscription_to_panel(db, user, sub)

    return BulkUserResult(
        user_id=user.id,
        success=True,
        message=f'Subscription extended by {days} days',
        username=user.username,
    )


async def _do_cancel_subscription(
    db: AsyncSession,
    user: User,
    params: BulkActionParams,
    dry_run: bool,
    sub_override: Subscription | None = None,
) -> BulkUserResult:
    sub = _resolve_subscription(user, sub_override)
    if not sub:
        return BulkUserResult(user_id=user.id, success=False, message='No subscription found', username=user.username)

    if dry_run:
        return BulkUserResult(
            user_id=user.id,
            success=True,
            message='Would cancel subscription',
            username=user.username,
        )

    sub.status = SubscriptionStatus.EXPIRED.value
    sub.end_date = datetime.now(UTC)
    # For daily tariffs: mark as paused to prevent auto-resume by DailySubscriptionService
    if sub.tariff and getattr(sub.tariff, 'is_daily', False):
        sub.is_daily_paused = True
    await db.commit()
    await db.refresh(sub)
    await _sync_subscription_to_panel(db, user, sub)

    return BulkUserResult(
        user_id=user.id,
        success=True,
        message='Subscription cancelled',
        username=user.username,
    )


async def _do_activate_subscription(
    db: AsyncSession,
    user: User,
    params: BulkActionParams,
    dry_run: bool,
    sub_override: Subscription | None = None,
) -> BulkUserResult:
    sub = _resolve_subscription(user, sub_override)
    if not sub:
        return BulkUserResult(user_id=user.id, success=False, message='No subscription found', username=user.username)

    # Проверка дубликата в мультитарифном режиме
    if settings.is_multi_tariff_enabled() and sub.tariff_id:
        from app.database.crud.subscription import get_subscription_by_user_and_tariff

        existing = await get_subscription_by_user_and_tariff(db, user.id, sub.tariff_id)
        if existing and existing.id != sub.id:
            return BulkUserResult(
                user_id=user.id,
                success=False,
                message='Cannot activate: user already has an active subscription for this tariff',
                username=user.username,
            )

    if dry_run:
        return BulkUserResult(
            user_id=user.id,
            success=True,
            message='Would activate subscription',
            username=user.username,
        )

    sub.status = SubscriptionStatus.ACTIVE.value
    if sub.end_date and sub.end_date <= datetime.now(UTC):
        # Extend by 30 days if expired
        sub.end_date = datetime.now(UTC) + timedelta(days=30)
    await db.commit()
    await db.refresh(sub)
    await _sync_subscription_to_panel(db, user, sub)

    return BulkUserResult(
        user_id=user.id,
        success=True,
        message='Subscription activated',
        username=user.username,
    )


async def _do_change_tariff(
    db: AsyncSession,
    user: User,
    params: BulkActionParams,
    tariff: Tariff,
    dry_run: bool,
    sub_override: Subscription | None = None,
) -> BulkUserResult:
    sub = _resolve_subscription(user, sub_override)
    if not sub:
        return BulkUserResult(user_id=user.id, success=False, message='No subscription found', username=user.username)

    # Проверка дубликата в мультитарифном режиме
    if settings.is_multi_tariff_enabled() and tariff.id != sub.tariff_id:
        from app.database.crud.subscription import get_subscription_by_user_and_tariff

        existing = await get_subscription_by_user_and_tariff(db, user.id, tariff.id)
        if existing and existing.id != sub.id:
            return BulkUserResult(
                user_id=user.id,
                success=False,
                message='User already has an active subscription for the target tariff',
                username=user.username,
            )

    if dry_run:
        return BulkUserResult(
            user_id=user.id,
            success=True,
            message=f'Would change tariff to {tariff.name}',
            username=user.username,
        )

    # Preserve extra purchased devices above the old tariff's base limit
    from app.database.crud.subscription import calc_device_limit_on_tariff_switch

    old_tariff = await get_tariff_by_id(db, sub.tariff_id) if sub.tariff_id else None

    sub.tariff_id = tariff.id
    sub.traffic_limit_gb = tariff.traffic_limit_gb
    sub.device_limit = calc_device_limit_on_tariff_switch(
        current_device_limit=sub.device_limit,
        old_tariff_device_limit=old_tariff.device_limit if old_tariff else None,
        new_tariff_device_limit=tariff.device_limit,
        max_device_limit=tariff.max_device_limit,
    )
    # Set squads from tariff
    sub.connected_squads = tariff.allowed_squads or []

    # Convert trial subscription to paid when switching to a non-trial tariff
    if sub.is_trial and not tariff.is_trial_available:
        sub.is_trial = False
        if sub.end_date and sub.end_date > datetime.now(UTC):
            sub.status = SubscriptionStatus.ACTIVE.value

    # Reset purchased traffic on tariff change
    await db.execute(sa_delete(TrafficPurchase).where(TrafficPurchase.subscription_id == sub.id))
    sub.purchased_traffic_gb = 0
    sub.traffic_reset_at = None

    if settings.RESET_TRAFFIC_ON_TARIFF_SWITCH:
        sub.traffic_used_gb = 0.0

    # Record tariff change transaction
    from app.database.crud.transaction import create_transaction

    await create_transaction(
        db=db,
        user_id=user.id,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=0,
        description=f"Смена тарифа (массовое действие) на '{tariff.name}'",
        commit=False,
    )

    await db.commit()
    await db.refresh(sub)

    # Sync to RemnaWave panel
    try:
        await _sync_subscription_to_panel(
            db,
            user,
            sub,
            reset_traffic=settings.RESET_TRAFFIC_ON_TARIFF_SWITCH,
            reset_traffic_reason='смена тарифа (bulk action)',
        )
    except Exception as e:
        logger.error('Failed to sync tariff switch with RemnaWave', user_id=user.id, error=e)

    return BulkUserResult(
        user_id=user.id,
        success=True,
        message=f'Tariff changed to {tariff.name}',
        username=user.username,
    )


async def _do_add_traffic(
    db: AsyncSession,
    user: User,
    params: BulkActionParams,
    dry_run: bool,
    sub_override: Subscription | None = None,
) -> BulkUserResult:
    traffic_gb = params.traffic_gb  # already validated
    sub = _resolve_subscription(user, sub_override)
    if not sub:
        return BulkUserResult(user_id=user.id, success=False, message='No subscription found', username=user.username)

    if dry_run:
        return BulkUserResult(
            user_id=user.id,
            success=True,
            message=f'Would add {traffic_gb} GB traffic',
            username=user.username,
        )

    await add_subscription_traffic(db, sub, traffic_gb)
    # Reactivate subscription if it was LIMITED/EXPIRED
    await reactivate_subscription(db, sub)
    await db.refresh(sub)

    await _sync_subscription_to_panel(db, user, sub)

    # Explicitly enable user on panel (PATCH may not clear LIMITED status)
    _enable_uuid = sub.remnawave_uuid if settings.is_multi_tariff_enabled() else getattr(user, 'remnawave_uuid', None)
    if _enable_uuid and sub.status == 'active':
        try:
            from app.services.subscription_service import SubscriptionService

            subscription_service = SubscriptionService()
            await subscription_service.enable_remnawave_user(_enable_uuid)
        except Exception:
            pass  # "User already enabled" is expected for active subscriptions

    return BulkUserResult(
        user_id=user.id,
        success=True,
        message=f'Added {traffic_gb} GB traffic',
        username=user.username,
    )


async def _do_add_balance(
    db: AsyncSession,
    user: User,
    params: BulkActionParams,
    dry_run: bool,
) -> BulkUserResult:
    amount_kopeks = params.amount_kopeks  # already validated

    if dry_run:
        return BulkUserResult(
            user_id=user.id,
            success=True,
            message=f'Would add {amount_kopeks / 100:.2f}₽ to balance',
            username=user.username,
        )

    success = await add_user_balance(
        db=db,
        user=user,
        amount_kopeks=amount_kopeks,
        description=params.balance_description,
        create_transaction=True,
        transaction_type=TransactionType.DEPOSIT,
        payment_method=PaymentMethod.MANUAL,
    )
    if not success:
        return BulkUserResult(
            user_id=user.id,
            success=False,
            message='Failed to add balance',
            username=user.username,
        )

    return BulkUserResult(
        user_id=user.id,
        success=True,
        message=f'Added {amount_kopeks / 100:.2f}₽ to balance',
        username=user.username,
    )


async def _do_assign_promo_group(
    db: AsyncSession,
    user: User,
    params: BulkActionParams,
    dry_run: bool,
) -> BulkUserResult:
    promo_group_id = params.promo_group_id  # may be None (= remove)

    if dry_run:
        action_msg = f'Would assign promo group {promo_group_id}' if promo_group_id else 'Would remove promo group'
        return BulkUserResult(user_id=user.id, success=True, message=action_msg, username=user.username)

    # Delete existing M2M entries
    await db.execute(sa_delete(UserPromoGroup).where(UserPromoGroup.user_id == user.id))

    if promo_group_id is not None:
        db.add(
            UserPromoGroup(
                user_id=user.id,
                promo_group_id=promo_group_id,
                assigned_by='admin',
            )
        )

    await db.flush()
    await sync_user_primary_promo_group(db, user.id)
    await db.commit()
    await db.refresh(user)

    action_msg = f'Promo group set to {promo_group_id}' if promo_group_id else 'Promo group removed'
    return BulkUserResult(user_id=user.id, success=True, message=action_msg, username=user.username)


async def _do_set_devices(
    db: AsyncSession,
    user: User,
    params: BulkActionParams,
    dry_run: bool,
    sub_override: Subscription | None = None,
) -> BulkUserResult:
    device_limit = params.device_limit  # already validated
    sub = _resolve_subscription(user, sub_override)
    if not sub:
        return BulkUserResult(user_id=user.id, success=False, message='No subscription found', username=user.username)

    if dry_run:
        return BulkUserResult(
            user_id=user.id,
            success=True,
            message=f'Would set devices to {device_limit}',
            username=user.username,
        )

    sub.device_limit = device_limit
    await db.commit()
    await db.refresh(sub)
    await _sync_subscription_to_panel(db, user, sub)

    return BulkUserResult(
        user_id=user.id,
        success=True,
        message=f'Set devices to {device_limit}',
        username=user.username,
    )


async def _do_delete_subscription(
    db: AsyncSession,
    user: User,
    params: BulkActionParams,
    dry_run: bool,
    sub_override: Subscription | None = None,
) -> BulkUserResult:
    sub = _resolve_subscription(user, sub_override)
    if not sub:
        return BulkUserResult(user_id=user.id, success=False, message='No subscription found', username=user.username)

    tariff_name = sub.tariff.name if sub.tariff else f'#{sub.id}'

    # Protect active paid subscriptions from accidental deletion
    if sub.is_active and not sub.is_trial and not params.force_delete_active_paid:
        return BulkUserResult(
            user_id=user.id,
            success=False,
            message=f'Skipped: {tariff_name} is active and paid (enable force_delete_active_paid to override)',
            username=user.username,
            subscriptions=_build_subscription_info(getattr(user, 'subscriptions', None) or []),
        )

    if dry_run:
        return BulkUserResult(
            user_id=user.id,
            success=True,
            message=f'Would delete subscription: {tariff_name}',
            username=user.username,
        )

    # Deactivate in RemnaWave panel first
    _sub_uuid = sub.remnawave_uuid if settings.is_multi_tariff_enabled() else getattr(user, 'remnawave_uuid', None)
    if _sub_uuid:
        try:
            from app.services.subscription_service import SubscriptionService

            subscription_service = SubscriptionService()
            await subscription_service.disable_remnawave_user(_sub_uuid)
        except Exception as e:
            logger.warning('Failed to disable user in RemnaWave during subscription delete', error=e)

    # Delete related records then subscription
    await db.execute(sa_delete(SubscriptionServer).where(SubscriptionServer.subscription_id == sub.id))
    await db.execute(sa_delete(TrafficPurchase).where(TrafficPurchase.subscription_id == sub.id))
    await db.execute(sa_delete(Subscription).where(Subscription.id == sub.id))
    await db.commit()

    return BulkUserResult(
        user_id=user.id,
        success=True,
        message=f'Subscription deleted: {tariff_name}',
        username=user.username,
        subscriptions=[],
    )


async def _do_delete_user(
    db: AsyncSession,
    user: User,
    params: BulkActionParams,
    dry_run: bool,
    sub_override: Subscription | None = None,
    *,
    admin_id: int = 0,
) -> BulkUserResult:
    username = user.username
    user_id = user.id

    if dry_run:
        return BulkUserResult(
            user_id=user_id,
            success=True,
            message='Would permanently delete user',
            username=username,
            subscriptions=[],
        )

    try:
        from app.services.user_service import UserService

        user_service = UserService()
        result = await user_service.delete_user_account(
            db,
            user_id,
            admin_id=admin_id,
            force_panel_delete=params.delete_from_panel,
        )

        if result.bot_deleted:
            panel_note = ''
            if params.delete_from_panel:
                panel_note = ' + panel' if result.panel_deleted else ' (panel failed)'
            return BulkUserResult(
                user_id=user_id,
                success=True,
                message=f'User deleted{panel_note}',
                username=username,
                subscriptions=[],
            )
        return BulkUserResult(
            user_id=user_id,
            success=False,
            message='Deletion failed',
            username=username,
            subscriptions=[],
        )
    except Exception as e:
        logger.exception('Failed to delete user in bulk action', user_id=user_id, error=str(e))
        return BulkUserResult(
            user_id=user_id,
            success=False,
            message='Delete failed: internal error',
            username=username,
            subscriptions=[],
        )


async def _do_grant_subscription(
    db: AsyncSession,
    user: User,
    params: BulkActionParams,
    tariff: Tariff,
    dry_run: bool,
) -> BulkUserResult:
    days = params.days  # already validated
    is_multi_tariff = settings.is_multi_tariff_enabled()
    subs = getattr(user, 'subscriptions', None) or []

    # Check for existing active subscription
    if is_multi_tariff:
        from app.database.crud.subscription import get_subscription_by_user_and_tariff

        existing = await get_subscription_by_user_and_tariff(db, user.id, tariff.id)
        if existing:
            return BulkUserResult(
                user_id=user.id,
                success=False,
                message='Already has subscription for this tariff',
                username=user.username,
                subscriptions=_build_subscription_info(subs),
            )
    else:
        active_sub = next((s for s in subs if s.is_active), None)
        if active_sub:
            return BulkUserResult(
                user_id=user.id,
                success=False,
                message='Already has an active subscription',
                username=user.username,
                subscriptions=_build_subscription_info(subs),
            )

    if dry_run:
        return BulkUserResult(
            user_id=user.id,
            success=True,
            message=f'Would grant subscription: {tariff.name} for {days} days',
            username=user.username,
            subscriptions=_build_subscription_info(subs),
        )

    connected_squads = tariff.allowed_squads or []

    from sqlalchemy.exc import IntegrityError

    try:
        new_sub = await create_paid_subscription(
            db=db,
            user_id=user.id,
            duration_days=days,
            traffic_limit_gb=tariff.traffic_limit_gb,
            device_limit=tariff.device_limit,
            is_trial=False,
            tariff_id=tariff.id,
            connected_squads=connected_squads,
        )
    except IntegrityError:
        await db.rollback()
        return BulkUserResult(
            user_id=user.id,
            success=False,
            message='Already has subscription for this tariff',
            username=user.username,
            subscriptions=_build_subscription_info(subs),
        )

    # Sync to RemnaWave panel
    try:
        await _sync_subscription_to_panel(db, user, new_sub)
    except Exception as e:
        logger.error('Failed to sync new subscription with RemnaWave', user_id=user.id, error=e)

    # Refresh user to get updated subscriptions list
    await db.refresh(user, ['subscriptions'])
    refreshed_subs = getattr(user, 'subscriptions', None) or []

    return BulkUserResult(
        user_id=user.id,
        success=True,
        message=f'Subscription granted: {tariff.name} for {days} days',
        username=user.username,
        subscriptions=_build_subscription_info(refreshed_subs),
    )


# ---------------------------------------------------------------------------
# Subscription info helper
# ---------------------------------------------------------------------------


def _build_subscription_info(subs: list[Subscription]) -> list[BulkSubscriptionInfo]:
    """Build a compact list of subscription info for the frontend."""
    result = []
    for sub in subs:
        days_remaining = 0
        if sub.end_date:
            delta = sub.end_date - datetime.now(UTC)
            days_remaining = max(0, delta.days)

        tariff_name = None
        if sub.tariff:
            tariff_name = sub.tariff.name

        result.append(
            BulkSubscriptionInfo(
                id=sub.id,
                tariff_id=sub.tariff_id,
                tariff_name=tariff_name,
                status=sub.status,
                days_remaining=days_remaining,
                traffic_used_gb=sub.traffic_used_gb or 0,
                traffic_limit_gb=sub.traffic_limit_gb or 0,
                device_limit=sub.device_limit or 0,
            )
        )
    return result


# ---------------------------------------------------------------------------
# Action dispatcher
# ---------------------------------------------------------------------------

_ACTION_HANDLERS = {
    BulkActionType.EXTEND_SUBSCRIPTION: _do_extend_subscription,
    BulkActionType.ADD_DAYS: _do_extend_subscription,
    BulkActionType.CANCEL_SUBSCRIPTION: _do_cancel_subscription,
    BulkActionType.ACTIVATE_SUBSCRIPTION: _do_activate_subscription,
    BulkActionType.ADD_TRAFFIC: _do_add_traffic,
    BulkActionType.ADD_BALANCE: _do_add_balance,
    BulkActionType.ASSIGN_PROMO_GROUP: _do_assign_promo_group,
    BulkActionType.SET_DEVICES: _do_set_devices,
    BulkActionType.DELETE_SUBSCRIPTION: _do_delete_subscription,
    BulkActionType.DELETE_USER: _do_delete_user,
}


# ---------------------------------------------------------------------------
# Pre-loop validation & tariff loading (shared by streaming / non-streaming)
# ---------------------------------------------------------------------------


async def _validate_and_prepare(
    db: AsyncSession,
    action: BulkActionType,
    params: BulkActionParams,
) -> Tariff | None:
    """Validate required params and pre-load tariff.  Returns the tariff or None."""
    if action in (BulkActionType.EXTEND_SUBSCRIPTION, BulkActionType.ADD_DAYS):
        _require_days(params)
    elif action == BulkActionType.CHANGE_TARIFF:
        _require_tariff_id(params)
    elif action == BulkActionType.ADD_TRAFFIC:
        _require_traffic_gb(params)
    elif action == BulkActionType.ADD_BALANCE:
        _require_amount_kopeks(params)
    elif action == BulkActionType.SET_DEVICES:
        _require_device_limit(params)
    elif action == BulkActionType.GRANT_SUBSCRIPTION:
        _require_tariff_id(params)
        _require_days(params)

    tariff: Tariff | None = None
    if action in (BulkActionType.CHANGE_TARIFF, BulkActionType.GRANT_SUBSCRIPTION):
        tariff = await get_tariff_by_id(db, params.tariff_id)
        if not tariff:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Tariff not found',
            )

    if action == BulkActionType.ASSIGN_PROMO_GROUP and params.promo_group_id is not None:
        result = await db.execute(select(PromoGroup).where(PromoGroup.id == params.promo_group_id))
        if not result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Promo group not found',
            )

    return tariff


async def _execute_for_user(
    db: AsyncSession,
    uid: int,
    action: BulkActionType,
    params: BulkActionParams,
    tariff: Tariff | None,
    dry_run: bool,
    admin_id: int = 0,
) -> BulkUserResult:
    """Execute the bulk action for a single user.  Handles exceptions internally."""
    try:
        user = await get_user_by_id(db, uid)
        if not user:
            return BulkUserResult(user_id=uid, success=False, message='User not found')

        if action == BulkActionType.CHANGE_TARIFF:
            result = await _do_change_tariff(db, user, params, tariff, dry_run)
        elif action == BulkActionType.GRANT_SUBSCRIPTION:
            result = await _do_grant_subscription(db, user, params, tariff, dry_run)
        elif action == BulkActionType.DELETE_USER:
            result = await _do_delete_user(db, user, params, dry_run, admin_id=admin_id)
        elif action in _ACTION_HANDLERS:
            handler = _ACTION_HANDLERS[action]
            result = await handler(db, user, params, dry_run)
        else:
            result = BulkUserResult(user_id=uid, success=False, message=f'Unknown action: {action}')

        # Attach subscription info to result when not already set
        if result.subscriptions is None:
            subs = getattr(user, 'subscriptions', None) or []
            result.subscriptions = _build_subscription_info(subs)

        return result

    except Exception as exc:
        logger.error('Bulk action failed for user', user_id=uid, action=action, error=str(exc))
        try:
            await db.rollback()
        except Exception:
            pass
        return BulkUserResult(user_id=uid, success=False, message='Action failed: internal error')


# Actions that operate on the user, not on a specific subscription
_USER_LEVEL_ACTIONS: set[BulkActionType] = {
    BulkActionType.ADD_BALANCE,
    BulkActionType.ASSIGN_PROMO_GROUP,
    BulkActionType.GRANT_SUBSCRIPTION,
    BulkActionType.DELETE_USER,
}


async def _execute_for_subscription(
    db: AsyncSession,
    sub_id: int,
    action: BulkActionType,
    params: BulkActionParams,
    tariff: Tariff | None,
    dry_run: bool,
) -> BulkUserResult:
    """Execute the bulk action for a single subscription.  Handles exceptions internally."""
    try:
        sub = await get_subscription_by_id(db, sub_id)
        if not sub:
            return BulkUserResult(user_id=0, subscription_id=sub_id, success=False, message='Subscription not found')

        user = sub.user
        if not user:
            return BulkUserResult(
                user_id=0, subscription_id=sub_id, success=False, message='User not found for subscription'
            )

        if action == BulkActionType.CHANGE_TARIFF:
            result = await _do_change_tariff(db, user, params, tariff, dry_run, sub_override=sub)
        elif action in _ACTION_HANDLERS:
            handler = _ACTION_HANDLERS[action]
            result = await handler(db, user, params, dry_run, sub_override=sub)
        else:
            result = BulkUserResult(
                user_id=user.id, subscription_id=sub_id, success=False, message=f'Unknown action: {action}'
            )

        result.subscription_id = sub_id

        # Attach subscription info — use the targeted sub directly to avoid
        # MissingGreenlet from lazy-loading user.subscriptions in async mode
        if result.subscriptions is None:
            result.subscriptions = _build_subscription_info([sub])

        return result

    except Exception as exc:
        logger.error('Bulk action failed for subscription', subscription_id=sub_id, action=action, error=str(exc))
        try:
            await db.rollback()
        except Exception:
            pass
        return BulkUserResult(user_id=0, subscription_id=sub_id, success=False, message='Action failed: internal error')


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post('/execute')
async def bulk_execute(
    request: BulkExecuteRequest,
    stream: bool = Query(default=False, description='Stream progress via SSE'),
    admin: User = Depends(require_permission('bulk_actions:execute')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Execute a bulk action on multiple users or subscriptions.

    When ``stream=true``, returns a ``text/event-stream`` response with
    per-user progress events followed by a final summary event.
    Otherwise returns a single JSON response (default, backwards-compatible).
    """
    action = request.action
    params = request.params
    dry_run = request.dry_run

    # Delete user requires elevated permission
    if action == BulkActionType.DELETE_USER:
        from app.services.permission_service import PermissionService

        allowed, _ = await PermissionService.check_permission(db, admin, 'users:delete')
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail='Permission users:delete is required for this action',
            )

    # Determine target mode: subscription_ids or user_ids
    use_subscription_ids = request.subscription_ids is not None

    if use_subscription_ids and action in _USER_LEVEL_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Action {action} operates on users, not subscriptions. Use user_ids instead.',
        )

    tariff = await _validate_and_prepare(db, action, params)

    if use_subscription_ids:
        sub_ids = list(dict.fromkeys(request.subscription_ids))

        if stream:
            return StreamingResponse(
                _stream_bulk_execute_subscriptions(db, sub_ids, action, params, tariff, dry_run, admin),
                media_type='text/event-stream',
            )

        # --- Non-streaming subscription path ---
        results: list[BulkUserResult] = []
        success_count = 0
        error_count = 0
        skipped_count = 0

        for sid in sub_ids:
            result = await _execute_for_subscription(db, sid, action, params, tariff, dry_run)

            results.append(result)
            if result.message in ('Subscription not found', 'User not found for subscription'):
                skipped_count += 1
            elif result.success:
                success_count += 1
            else:
                error_count += 1

        logger.info(
            'Bulk action completed (subscription mode)',
            admin_id=admin.id,
            action=action,
            total=len(sub_ids),
            success_count=success_count,
            error_count=error_count,
            skipped_count=skipped_count,
            dry_run=dry_run,
        )

        return BulkExecuteResponse(
            action=action,
            total=len(sub_ids),
            success_count=success_count,
            error_count=error_count,
            skipped_count=skipped_count,
            dry_run=dry_run,
            results=results,
        )

    # --- user_ids mode (original) ---
    user_ids = list(dict.fromkeys(request.user_ids))

    if stream:
        return StreamingResponse(
            _stream_bulk_execute(db, user_ids, action, params, tariff, dry_run, admin),
            media_type='text/event-stream',
        )

    results = []
    success_count = 0
    error_count = 0
    skipped_count = 0

    for uid in user_ids:
        result = await _execute_for_user(db, uid, action, params, tariff, dry_run, admin_id=admin.id)

        results.append(result)
        if result.message == 'User not found':
            skipped_count += 1
        elif result.success:
            success_count += 1
        else:
            error_count += 1

    logger.info(
        'Bulk action completed',
        admin_id=admin.id,
        action=action,
        total=len(user_ids),
        success_count=success_count,
        error_count=error_count,
        skipped_count=skipped_count,
        dry_run=dry_run,
    )

    return BulkExecuteResponse(
        action=action,
        total=len(user_ids),
        success_count=success_count,
        error_count=error_count,
        skipped_count=skipped_count,
        dry_run=dry_run,
        results=results,
    )


# ---------------------------------------------------------------------------
# SSE streaming generator
# ---------------------------------------------------------------------------


async def _stream_bulk_execute(
    db: AsyncSession,
    user_ids: list[int],
    action: BulkActionType,
    params: BulkActionParams,
    tariff: Tariff | None,
    dry_run: bool,
    admin: User,
):
    """Yield SSE events for each processed user, then a final summary."""
    total = len(user_ids)
    success_count = 0
    error_count = 0
    skipped_count = 0

    for i, uid in enumerate(user_ids):
        result = await _execute_for_user(db, uid, action, params, tariff, dry_run, admin_id=admin.id)

        if result.message == 'User not found':
            skipped_count += 1
        elif result.success:
            success_count += 1
        else:
            error_count += 1

        progress = {
            'type': 'progress',
            'current': i + 1,
            'total': total,
            'user_id': uid,
            'subscription_id': result.subscription_id,
            'success': result.success,
            'message': result.message,
            'username': result.username,
            'subscriptions': [s.model_dump() for s in result.subscriptions] if result.subscriptions else None,
        }
        yield f'data: {json.dumps(progress, ensure_ascii=False)}\n\n'

    logger.info(
        'Bulk action completed (streamed)',
        admin_id=admin.id,
        action=action,
        total=total,
        success_count=success_count,
        error_count=error_count,
        skipped_count=skipped_count,
        dry_run=dry_run,
    )

    summary = {
        'type': 'complete',
        'action': str(action),
        'total': total,
        'success_count': success_count,
        'error_count': error_count,
        'skipped_count': skipped_count,
        'dry_run': dry_run,
    }
    yield f'data: {json.dumps(summary, ensure_ascii=False)}\n\n'


async def _stream_bulk_execute_subscriptions(
    db: AsyncSession,
    sub_ids: list[int],
    action: BulkActionType,
    params: BulkActionParams,
    tariff: Tariff | None,
    dry_run: bool,
    admin: User,
):
    """Yield SSE events for each processed subscription, then a final summary."""
    total = len(sub_ids)
    success_count = 0
    error_count = 0
    skipped_count = 0

    for i, sid in enumerate(sub_ids):
        result = await _execute_for_subscription(db, sid, action, params, tariff, dry_run)

        if result.message in ('Subscription not found', 'User not found for subscription'):
            skipped_count += 1
        elif result.success:
            success_count += 1
        else:
            error_count += 1

        progress = {
            'type': 'progress',
            'current': i + 1,
            'total': total,
            'user_id': result.user_id,
            'subscription_id': sid,
            'success': result.success,
            'message': result.message,
            'username': result.username,
            'subscriptions': [s.model_dump() for s in result.subscriptions] if result.subscriptions else None,
        }
        yield f'data: {json.dumps(progress, ensure_ascii=False)}\n\n'

    logger.info(
        'Bulk action completed (streamed, subscription mode)',
        admin_id=admin.id,
        action=action,
        total=total,
        success_count=success_count,
        error_count=error_count,
        skipped_count=skipped_count,
        dry_run=dry_run,
    )

    summary = {
        'type': 'complete',
        'action': str(action),
        'total': total,
        'success_count': success_count,
        'error_count': error_count,
        'skipped_count': skipped_count,
        'dry_run': dry_run,
    }
    yield f'data: {json.dumps(summary, ensure_ascii=False)}\n\n'
