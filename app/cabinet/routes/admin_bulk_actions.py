"""Admin routes for bulk actions on users."""

from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import (
    add_subscription_traffic,
    extend_subscription,
    reactivate_subscription,
)
from app.database.crud.tariff import get_tariff_by_id
from app.database.crud.user import add_user_balance, get_user_by_id
from app.database.crud.user_promo_group import sync_user_primary_promo_group
from app.database.models import (
    PaymentMethod,
    PromoGroup,
    Subscription,
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


# ---------------------------------------------------------------------------
# Subscription resolver
# ---------------------------------------------------------------------------


def _resolve_subscription(user: User) -> Subscription | None:
    """Return the first active subscription or most recent one (multi/single tariff)."""
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
) -> BulkUserResult:
    days = params.days  # already validated
    sub = _resolve_subscription(user)
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
) -> BulkUserResult:
    sub = _resolve_subscription(user)
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
) -> BulkUserResult:
    sub = _resolve_subscription(user)
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
) -> BulkUserResult:
    sub = _resolve_subscription(user)
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
    if tariff.allowed_squads:
        sub.connected_squads = tariff.allowed_squads

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
) -> BulkUserResult:
    traffic_gb = params.traffic_gb  # already validated
    sub = _resolve_subscription(user)
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
        from app.services.subscription_service import SubscriptionService

        subscription_service = SubscriptionService()
        await subscription_service.enable_remnawave_user(_enable_uuid)

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
}


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post('/execute', response_model=BulkExecuteResponse)
async def bulk_execute(
    request: BulkExecuteRequest,
    admin: User = Depends(require_permission('users:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Execute a bulk action on multiple users."""
    # Deduplicate user IDs
    user_ids = list(dict.fromkeys(request.user_ids))
    action = request.action
    params = request.params
    dry_run = request.dry_run

    # --- Pre-loop validation of required params ---
    if action in (BulkActionType.EXTEND_SUBSCRIPTION, BulkActionType.ADD_DAYS):
        _require_days(params)
    elif action == BulkActionType.CHANGE_TARIFF:
        _require_tariff_id(params)
    elif action == BulkActionType.ADD_TRAFFIC:
        _require_traffic_gb(params)
    elif action == BulkActionType.ADD_BALANCE:
        _require_amount_kopeks(params)

    # Pre-load tariff once for change_tariff action
    tariff: Tariff | None = None
    if action == BulkActionType.CHANGE_TARIFF:
        tariff = await get_tariff_by_id(db, params.tariff_id)
        if not tariff:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Tariff not found',
            )

    # Pre-validate promo group exists (if assigning, not removing)
    if action == BulkActionType.ASSIGN_PROMO_GROUP and params.promo_group_id is not None:
        result = await db.execute(select(PromoGroup).where(PromoGroup.id == params.promo_group_id))
        if not result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Promo group not found',
            )

    # --- Per-user loop ---
    results: list[BulkUserResult] = []
    success_count = 0
    error_count = 0
    skipped_count = 0

    for uid in user_ids:
        try:
            user = await get_user_by_id(db, uid)
            if not user:
                results.append(BulkUserResult(user_id=uid, success=False, message='User not found'))
                skipped_count += 1
                continue

            if action == BulkActionType.CHANGE_TARIFF:
                result = await _do_change_tariff(db, user, params, tariff, dry_run)
            elif action in _ACTION_HANDLERS:
                handler = _ACTION_HANDLERS[action]
                result = await handler(db, user, params, dry_run)
            else:
                result = BulkUserResult(user_id=uid, success=False, message=f'Unknown action: {action}')

            results.append(result)
            if result.success:
                success_count += 1
            else:
                error_count += 1

        except Exception as exc:
            logger.error('Bulk action failed for user', user_id=uid, action=action, error=str(exc))
            try:
                await db.rollback()
            except Exception:
                pass
            results.append(BulkUserResult(user_id=uid, success=False, message=str(exc)))
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
