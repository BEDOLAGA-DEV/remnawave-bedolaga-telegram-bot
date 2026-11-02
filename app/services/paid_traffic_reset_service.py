"""Durable, single-attempt delivery of the reset purchased by a daily charge.

A lost reset response must never turn into another blind reset of new usage.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaidTrafficResetIntent, Subscription, User
from app.services.panel_sync.identity import find_foreign_panel_owner


logger = structlog.get_logger(__name__)
OPEN_STATES = ('pending', 'attempted')
MAX_CHECKS = 12
RESET_TIMEOUT_SECONDS = 15


async def stage_daily_reset(db: AsyncSession, subscription: Subscription, transaction, *, reason: str) -> int:
    """Part of the financial transaction; caller commits charge and intent together."""
    intent = PaidTrafficResetIntent(
        subscription_id=subscription.id,
        user_id=subscription.user_id,
        transaction_id=transaction.id,
        operation_key=f'daily:{subscription.id}:{subscription.last_daily_charge_at.isoformat()}',
        charge_at=subscription.last_daily_charge_at,
        reason=reason,
        state='pending',
    )
    db.add(intent)
    await db.flush()
    return intent.id


async def _locked(db: AsyncSession, intent_id: int):
    """User → subscription → intents, reread after each lock wait."""
    address = (
        await db.execute(
            select(PaidTrafficResetIntent.user_id, PaidTrafficResetIntent.subscription_id).where(
                PaidTrafficResetIntent.id == intent_id
            )
        )
    ).first()
    if address is None:
        return None
    if address.user_id is None or address.subscription_id is None:
        intent = await db.get(PaidTrafficResetIntent, intent_id, with_for_update=True, populate_existing=True)
        return None, None, intent, [intent] if intent is not None else []
    user = (
        await db.scalars(
            select(User).where(User.id == address.user_id).with_for_update().execution_options(populate_existing=True)
        )
    ).first()
    sub = (
        await db.scalars(
            select(Subscription)
            .where(Subscription.id == address.subscription_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).first()
    intents = list(
        (
            await db.scalars(
                select(PaidTrafficResetIntent)
                .where(PaidTrafficResetIntent.id == intent_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    intent = next((item for item in intents if item.id == intent_id), None)
    return user, sub, intent, intents


def _target(user, sub):
    return sub.remnawave_id if settings.is_multi_tariff_enabled() else user.remnawave_id


def _identity_matches(intent, panel_user) -> bool:
    return (
        bool(intent.target_short_uuid)
        and isinstance(intent.target_short_uuid, str)
        and panel_user is not None
        and panel_user.id == intent.target_panel_id
        and panel_user.short_uuid == intent.target_short_uuid
    )


def _valid(user, sub, intent) -> bool:
    return (
        user is not None
        and sub is not None
        and sub.user_id == intent.user_id
        and user.status == 'active'
        and sub.status in ('active', 'limited')
        and not sub.is_trial
        and not sub.is_daily_paused
        and not sub.grace_session_open
        and sub.end_date > datetime.now(UTC)
        and sub.last_daily_charge_at == intent.charge_at
    )


def _block(intent, reason: str) -> None:
    intent.state = 'blocked'
    intent.last_error = reason
    logger.warning(
        'Paid daily traffic reset blocked; inspect before retrying',
        subscription_id=intent.subscription_id,
        intent_id=intent.id,
        reason=reason,
    )


async def _confirm(db, sub, intent, panel_user, *, update_subscription: bool = True) -> None:
    intent.state = 'confirmed'
    intent.confirmed_at = datetime.now(UTC)
    intent.confirmed_last_reset_at = panel_user.last_traffic_reset_at
    intent.last_error = None
    intent.activation_pending = update_subscription
    intent_id = intent.id
    # A late acknowledgement may contain new consumption. Never invent usage=0.
    if update_subscription and panel_user.user_traffic is not None:
        sub.traffic_used_gb = panel_user.used_traffic_bytes / 1024**3
    await db.commit()
    if update_subscription:
        await _activate_confirmed(db, intent_id)


async def _activate_confirmed(db: AsyncSession, intent_id: int) -> None:
    from app.services.subscription_service import SubscriptionService

    locked = await _locked(db, intent_id)
    if locked is None:
        return
    user, sub, intent, _ = locked
    if intent is None or intent.state != 'confirmed' or not intent.activation_pending:
        await db.rollback()
        return
    if (
        not _valid(user, sub, intent)
        or _target(user, sub) != intent.target_panel_id
        or (sub.remnawave_short_uuid and sub.remnawave_short_uuid != intent.target_short_uuid)
        or await find_foreign_panel_owner(
            db, user, sub, intent.target_panel_id, multi_tariff=settings.is_multi_tariff_enabled()
        )
        is not None
    ):
        intent.activation_pending = False
        intent.last_error = 'activation_owner_or_cycle_changed'
        await db.commit()
        logger.warning('Paid traffic reset activation skipped after identity/cycle change', intent_id=intent_id)
        return
    try:
        async with SubscriptionService().get_api_client() as api:
            async with asyncio.timeout(RESET_TIMEOUT_SECONDS):
                current = await api.get_user_by_id(intent.target_panel_id)
                if not _identity_matches(intent, current):
                    raise RuntimeError('Reset activation identity mismatch')
                current_status = str(getattr(current.status, 'value', current.status)).lower()
                if current_status == 'limited':
                    if current.user_traffic is None:
                        raise RuntimeError('Reset activation awaits known usage')
                    if current.traffic_limit_bytes > 0 and current.used_traffic_bytes >= current.traffic_limit_bytes:
                        # New consumption may have exhausted the freshly granted quota.
                        intent.activation_pending = False
                        intent.last_error = 'new_quota_already_exhausted'
                        await db.commit()
                        return
                    current = await api.enable_user(intent.target_panel_id)
                    if not _identity_matches(intent, current):
                        raise RuntimeError('Reset activation acknowledgement identity mismatch')
                    current_status = str(getattr(current.status, 'value', current.status)).lower()
                if current_status != 'active':
                    # An external manual disable must not be overwritten.
                    intent.last_error = 'activation_external_status_changed'
                    logger.warning('Paid traffic reset activation deferred to external status', intent_id=intent_id)
                intent.activation_pending = False
                await db.commit()
    except Exception:
        await db.rollback()
        from sqlalchemy import update

        await db.execute(
            update(PaidTrafficResetIntent)
            .where(PaidTrafficResetIntent.id == intent_id)
            .values(next_check_at=datetime.now(UTC) + timedelta(minutes=2), last_error='activation_retry_pending')
        )
        await db.commit()
        logger.warning(
            'Paid daily reset acknowledged; activation will retry without resetting again', intent_id=intent_id
        )


def _marker_advanced(intent, snapshot) -> bool:
    return (
        intent.baseline_observed
        and snapshot.last_traffic_reset_at is not None
        and (intent.baseline_last_reset_at is None or snapshot.last_traffic_reset_at > intent.baseline_last_reset_at)
    )


async def _reconcile_attempted(db, user, sub, intent) -> bool:
    """Even terminal ambiguity gets low-frequency GET-only reconciliation."""
    from app.services.subscription_service import SubscriptionService

    try:
        async with SubscriptionService().get_api_client() as api:
            async with asyncio.timeout(RESET_TIMEOUT_SECONDS):
                snapshot = await api.get_user_by_id(intent.target_panel_id)
        if _identity_matches(intent, snapshot) and _marker_advanced(intent, snapshot):
            owns_current_cycle = (
                _valid(user, sub, intent)
                and _target(user, sub) == intent.target_panel_id
                and (not sub.remnawave_short_uuid or sub.remnawave_short_uuid == intent.target_short_uuid)
                and await find_foreign_panel_owner(
                    db, user, sub, intent.target_panel_id, multi_tariff=settings.is_multi_tariff_enabled()
                )
                is None
            )
            # Resolving an old/deleted owner's ambiguity must not write new-cycle rights.
            await _confirm(db, sub, intent, snapshot, update_subscription=owns_current_cycle)
            return True
        intent.checks = min(intent.checks + 1, MAX_CHECKS)
        if intent.checks >= MAX_CHECKS or not _identity_matches(intent, snapshot):
            reason = 'reset_outcome_ambiguous' if _identity_matches(intent, snapshot) else 'panel_identity_changed'
            if intent.state != 'blocked' or intent.last_error != reason:
                _block(intent, reason)
            intent.next_check_at = datetime.now(UTC) + timedelta(hours=1)
        else:
            intent.last_error = 'reset_outcome_ambiguous'
            intent.next_check_at = datetime.now(UTC) + timedelta(minutes=10)
        await db.commit()
        return False
    except BaseException:
        await db.rollback()
        raise


async def deliver_daily_reset(db: AsyncSession, intent_id: int) -> bool:
    """Canonical panel sync must have succeeded first. Never charges the user."""
    from app.services.subscription_service import SubscriptionService

    locked = await _locked(db, intent_id)
    if locked is None:
        return False
    user, sub, intent, intents = locked
    if intent is None:
        await db.rollback()
        return False
    if intent.state == 'attempted' or (intent.state == 'blocked' and intent.attempted_at is not None):
        return await _reconcile_attempted(db, user, sub, intent)
    if intent.state not in OPEN_STATES:
        confirmed = intent.state == 'confirmed'
        await db.rollback()
        if confirmed:
            await _activate_confirmed(db, intent_id)
        return confirmed
    if not _valid(user, sub, intent):
        if intent.state == 'pending' and sub is not None and sub.last_daily_charge_at != intent.charge_at:
            intent.state = 'superseded'
        else:
            _block(intent, 'owner_or_paid_cycle_changed')
        await db.commit()
        return False
    panel_id = _target(user, sub)
    if panel_id is None:
        intent.next_check_at = datetime.now(UTC) + timedelta(minutes=2)
        await db.commit()
        return False
    if (
        await find_foreign_panel_owner(db, user, sub, panel_id, multi_tariff=settings.is_multi_tariff_enabled())
        is not None
    ):
        _block(intent, 'panel_account_owned_elsewhere')
        await db.commit()
        return False
    if intent.target_panel_id is not None and intent.target_panel_id != panel_id:
        _block(intent, 'panel_identity_changed')
        await db.commit()
        return False
    other_target = (
        await db.scalars(
            select(PaidTrafficResetIntent.id)
            .where(
                PaidTrafficResetIntent.target_panel_id == panel_id,
                PaidTrafficResetIntent.id != intent.id,
                PaidTrafficResetIntent.attempted_at.is_not(None),
                PaidTrafficResetIntent.state.in_(('attempted', 'blocked')),
            )
            .limit(1)
        )
    ).first()
    service = SubscriptionService()
    if not service.is_configured:
        await db.rollback()
        return False
    claimed_here = False
    dispatched = False
    try:
        async with service.get_api_client() as api:
            async with asyncio.timeout(RESET_TIMEOUT_SECONDS):
                snapshot = await api.get_user_by_id(panel_id)
            if (
                snapshot is None
                or snapshot.id != panel_id
                or not isinstance(snapshot.short_uuid, str)
                or not snapshot.short_uuid
            ):
                _block(intent, 'panel_account_missing')
                await db.commit()
                return False
            if intent.target_short_uuid is not None and not _identity_matches(intent, snapshot):
                _block(intent, 'panel_identity_changed')
                await db.commit()
                return False
            # Explicit known identity is bound before attempt and checked again later.
            if sub.remnawave_short_uuid and snapshot.short_uuid != sub.remnawave_short_uuid:
                _block(intent, 'panel_short_identity_mismatch')
                await db.commit()
                return False
            if (
                intent.target_panel_id is not None
                and _identity_matches(intent, snapshot)
                and _marker_advanced(intent, snapshot)
            ):
                await _confirm(db, sub, intent, snapshot)
                return True
            intent.target_panel_id = panel_id
            intent.target_short_uuid = snapshot.short_uuid
            intent.baseline_last_reset_at = snapshot.last_traffic_reset_at
            intent.baseline_observed = snapshot.last_traffic_reset_at_known
            if other_target is not None:
                # Observe a baseline while an older ambiguous request blocks POST.
                # A later read can prove this paid request was covered by a reset.
                intent.last_error = 'panel_target_has_unresolved_reset'
                intent.next_check_at = datetime.now(UTC) + timedelta(minutes=10)
                await db.commit()
                return False
            intent.attempted_at = datetime.now(UTC)
            intent.state = 'attempted'
            intent.next_check_at = datetime.now(UTC) + timedelta(minutes=2)
            await db.commit()
            claimed_here = True
            # The first commit makes crash recovery safe. Re-lock before POST so a
            # merge/delete cannot change the owner in the claim→send gap.
            locked = await _locked(db, intent_id)
            if locked is None:
                return False
            user, sub, intent, _ = locked
            if intent is None or intent.state != 'attempted':
                await db.rollback()
                return False
            if (
                not _valid(user, sub, intent)
                or _target(user, sub) != intent.target_panel_id
                or (sub.remnawave_short_uuid and sub.remnawave_short_uuid != intent.target_short_uuid)
                or await find_foreign_panel_owner(
                    db, user, sub, intent.target_panel_id, multi_tariff=settings.is_multi_tariff_enabled()
                )
                is not None
            ):
                intent.attempted_at = None
                _block(intent, 'owner_identity_or_cycle_changed_before_send')
                await db.commit()
                return False
            # Row locks stay held across this bounded request. The API transport
            # itself must also disable automatic retries for reset-traffic.
            async with asyncio.timeout(RESET_TIMEOUT_SECONDS):
                current = await api.get_user_by_id(intent.target_panel_id)
                if not _identity_matches(intent, current):
                    intent.attempted_at = None
                    _block(intent, 'panel_identity_changed_before_send')
                    await db.commit()
                    return False
                if (
                    intent.baseline_observed
                    and current.last_traffic_reset_at is not None
                    and (
                        intent.baseline_last_reset_at is None
                        or current.last_traffic_reset_at > intent.baseline_last_reset_at
                    )
                ):
                    # Another acknowledged reset since our snapshot already granted
                    # this quota. Do not erase consumption accumulated after it.
                    await _confirm(db, sub, intent, current)
                    return True
                dispatched = True
                response = await api.reset_user_traffic(intent.target_panel_id, enrich=False)
            if not _identity_matches(intent, response):
                _block(intent, 'reset_ack_identity_mismatch')
                await db.commit()
                return False
            # Parsed matching2xx is an ACK even when provider omits reset marker.
            await _confirm(db, sub, intent, response)
            return True
    except BaseException:
        # The committed attempted marker survives rollback/cancellation. An
        # exception after POST is not grounds for a second destructive request.
        await db.rollback()
        if claimed_here and not dispatched:
            from sqlalchemy import update

            await db.execute(
                update(PaidTrafficResetIntent)
                .where(PaidTrafficResetIntent.id == intent_id, PaidTrafficResetIntent.state == 'attempted')
                .values(
                    state='pending',
                    attempted_at=None,
                    next_check_at=datetime.now(UTC) + timedelta(minutes=2),
                    last_error='pre_reset_read_failed',
                )
            )
            await db.commit()
        logger.warning('Paid daily traffic reset deferred for reconciliation', intent_id=intent_id, exc_info=True)
        raise


async def process_pending_daily_resets(session_factory, *, limit: int = 25) -> None:
    """Recover committed requests after queue loss/restart, with bounded due scans."""
    from sqlalchemy import and_, or_, update

    from app.database.crud.subscription import get_subscription_by_id
    from app.services.subscription_service import SubscriptionService

    async with session_factory() as db:
        ids = list(
            (
                await db.scalars(
                    select(PaidTrafficResetIntent.id)
                    .where(
                        or_(
                            PaidTrafficResetIntent.state.in_(OPEN_STATES),
                            and_(
                                PaidTrafficResetIntent.state == 'blocked',
                                PaidTrafficResetIntent.attempted_at.is_not(None),
                            ),
                            and_(
                                PaidTrafficResetIntent.state == 'confirmed',
                                PaidTrafficResetIntent.activation_pending.is_(True),
                            ),
                        ),
                        or_(
                            PaidTrafficResetIntent.next_check_at.is_(None),
                            PaidTrafficResetIntent.next_check_at <= datetime.now(UTC),
                        ),
                    )
                    .order_by(PaidTrafficResetIntent.next_check_at.asc().nullsfirst(), PaidTrafficResetIntent.id)
                    .limit(limit)
                )
            ).all()
        )
    for intent_id in ids:
        async with session_factory() as db:
            try:
                locked = await _locked(db, intent_id)
                if locked is None:
                    continue
                user, sub, intent, _ = locked
                if intent is None or (
                    intent.state not in OPEN_STATES
                    and not intent.activation_pending
                    and not (intent.state == 'blocked' and intent.attempted_at is not None)
                ):
                    continue
                pending = intent.state == 'pending'
                valid = _valid(user, sub, intent)
                sub_id = intent.subscription_id
                intent.next_check_at = datetime.now(UTC) + (
                    timedelta(hours=1) if intent.state == 'blocked' else timedelta(minutes=2)
                )
                await db.commit()
                if pending and valid:
                    sub = await get_subscription_by_id(db, sub_id)
                    async with asyncio.timeout(30):
                        result = await SubscriptionService().sync_remnawave_user(db, sub, reset_traffic=False)
                    if result is None:
                        raise RuntimeError('Daily reset awaits canonical panel synchronization')
                await deliver_daily_reset(db, intent_id)
            except Exception:
                await db.rollback()
                await db.execute(
                    update(PaidTrafficResetIntent)
                    .where(
                        PaidTrafficResetIntent.id == intent_id,
                        PaidTrafficResetIntent.state.in_(OPEN_STATES),
                    )
                    .values(next_check_at=datetime.now(UTC) + timedelta(minutes=2))
                )
                await db.commit()
                logger.warning('Paid daily traffic reset recovery deferred', intent_id=intent_id, exc_info=True)
