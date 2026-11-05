"""Transactional traffic accounting; panel counters are observations, not grants.

A resettable counter alone is insufficient. Account creation time is part of
identity so a recreated account cannot silently consume or restore old grants.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from app.services.traffic_accounting import AccountingConflict, TrafficAccount, observe_usage


@dataclass(frozen=True)
class CounterSnapshot:
    identity: str
    lifetime_bytes: int
    used_bytes: int


class AccountingAccountMissing(AccountingConflict):
    """Only a confirmed missing recorded account permits explicit recreation."""


def read_counters(panel) -> CounterSnapshot:
    traffic = getattr(panel, 'user_traffic', None)
    created_at = getattr(panel, 'created_at', None)
    panel_id = getattr(panel, 'id', None)
    if (
        traffic is None
        or not getattr(traffic, 'counters_known', True)
        or not isinstance(created_at, datetime)
        or created_at.tzinfo is None
        or not panel_id
    ):
        raise AccountingConflict('Incomplete panel traffic snapshot')
    lifetime = getattr(traffic, 'lifetime_used_traffic_bytes', None)
    used = getattr(traffic, 'used_traffic_bytes', None)
    if type(lifetime) is not int or type(used) is not int or not 0 <= used <= lifetime:
        raise AccountingConflict('Invalid panel traffic snapshot counters')
    return CounterSnapshot(f'{panel_id}@{created_at.astimezone(UTC).isoformat()}', lifetime, used)


def settle_snapshot(account: TrafficAccount, panel, *, now: datetime, exclude_usage: bool = False) -> TrafficAccount:
    snapshot = read_counters(panel)
    # Validate even when excluding grace traffic; grace cannot hide a recreated
    # account, regressing cumulative counter, or inconsistent used counter.
    observed = observe_usage(account, snapshot.identity, snapshot.lifetime_bytes, snapshot.used_bytes, now)
    if not exclude_usage:
        return observed
    baseline = replace(account, lifetime_bytes=snapshot.lifetime_bytes, used_bytes=snapshot.used_bytes)
    return observe_usage(baseline, snapshot.identity, snapshot.lifetime_bytes, snapshot.used_bytes, now)


async def traffic_accounting_summary(db, subscription_id: int, now: datetime | None = None) -> dict:
    """Read the last settled balances without locking, committing or panel IO."""
    from sqlalchemy import select

    from app.database.models import Subscription, TrafficAccountingState, TrafficAllowance
    from app.services.traffic_accounting_store import _aware

    moment = now or datetime.now(UTC)
    # Read state and grants in one statement: no mixed revisions when a payment
    # commits between two separate SELECTs at READ COMMITTED isolation.
    rows = (
        await db.execute(
            select(
                TrafficAccountingState.phase,
                Subscription.traffic_limit_gb,
                TrafficAccountingState.next_refresh_at,
                TrafficAllowance.operation_key,
                TrafficAllowance.remaining_bytes,
                TrafficAllowance.expires_at,
            )
            .join(Subscription, Subscription.id == TrafficAccountingState.subscription_id)
            .outerjoin(TrafficAllowance, TrafficAllowance.subscription_id == TrafficAccountingState.subscription_id)
            .where(TrafficAccountingState.subscription_id == subscription_id)
        )
    ).all()
    summary = dict(
        traffic_accounting_mode='legacy',
        traffic_remaining_gb=None,
        base_traffic_remaining_gb=None,
        next_traffic_refresh_at=None,
        purchase_remaining={},
    )
    # Money may commit new nominal rights before the durable intent can be
    # settled against a fresh panel snapshot. Neither old finite allowances nor
    # an unlimited watermark describe the new entitlement in that interval.
    if (
        rows
        and rows[0].phase in ('ready', 'unlimited')
        and (rows[0].phase == 'unlimited' or rows[0].traffic_limit_gb == 0)
    ):
        return {**summary, 'traffic_accounting_mode': 'managed'}
    if not rows or rows[0].phase != 'ready':
        return summary
    total = base = 0
    packages = {}
    for row in rows:
        if row.operation_key is None:
            continue
        remaining = row.remaining_bytes if _aware(row.expires_at) > moment else 0
        total += remaining
        if row.operation_key.startswith('base:'):
            base += remaining
        elif row.operation_key.startswith('purchase:'):
            packages[int(row.operation_key.removeprefix('purchase:'))] = remaining / 1024**3
    return dict(
        traffic_accounting_mode='managed',
        traffic_remaining_gb=total / 1024**3,
        base_traffic_remaining_gb=base / 1024**3,
        next_traffic_refresh_at=rows[0].next_refresh_at,
        purchase_remaining=packages,
    )


async def lock_accounting_subscription(db, subscription_id: int):
    """User → grace advisory lease → Subscription; caller retains transaction.

    Grace takes its advisory lock before changing Subscription. Taking the row
    lock first would deadlock against an overlay waiting to update that row.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.database.models import Subscription, User
    from app.services.grace_access_runtime import lock_grace_sensitive_panel_updates

    owner_id = await db.scalar(select(Subscription.user_id).where(Subscription.id == subscription_id))
    if owner_id is None:
        raise AccountingConflict('Subscription does not exist')
    await db.execute(select(User.id).where(User.id == owner_id).with_for_update())
    open_grace = await lock_grace_sensitive_panel_updates(db, (subscription_id,))
    subscription = await db.scalar(
        select(Subscription)
        .where(Subscription.id == subscription_id)
        .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if subscription is None:
        raise AccountingConflict('Subscription disappeared')
    db.info.setdefault('traffic_accounting_leases', {})[subscription_id] = db.sync_session.get_transaction()
    return subscription, bool(open_grace or subscription.grace_session_open)


async def _check_migration_preconditions(db, subscription, grace_open):
    from sqlalchemy import or_, select

    from app.database.models import PaidTrafficResetIntent

    if grace_open:
        raise AccountingConflict('Cannot migrate an open grace overlay')
    unresolved = await db.scalar(
        select(PaidTrafficResetIntent.id)
        .where(
            PaidTrafficResetIntent.subscription_id == subscription.id,
            or_(
                PaidTrafficResetIntent.state.in_(('pending', 'attempted', 'blocked')),
                PaidTrafficResetIntent.activation_pending.is_(True),
            ),
        )
        .limit(1)
    )
    if unresolved is not None:
        raise AccountingConflict('Resolve the previous paid reset before accounting migration')
    if not subscription.traffic_limit_gb or subscription.traffic_limit_gb <= 0:
        raise AccountingConflict('Only finite subscriptions can be migrated')


def _panel_id(subscription):
    from app.config import settings

    panel_id = subscription.remnawave_id if settings.is_multi_tariff_enabled() else subscription.user.remnawave_id
    # A newly linked first account may exist on Subscription before the owner
    # link is filled. Ownership is checked separately before any panel write.
    return panel_id or subscription.remnawave_id


async def _fresh_panel(db, api, subscription):
    from app.config import settings
    from app.services.panel_sync.identity import find_foreign_panel_owner

    panel_id = _panel_id(subscription)
    recovered_identity = None
    if not panel_id:
        from sqlalchemy import exists, select

        from app.database.models import TrafficAccountingState, TrafficBaseGrantIntent

        # Cleanup may clear both local links. Only a newly committed paid
        # intent may recover the exact previous epoch, never an email/username
        # match or an arbitrary panel ID. Missing accounts use normal recreation.
        state = await db.scalar(
            select(TrafficAccountingState).where(
                TrafficAccountingState.subscription_id == subscription.id,
                TrafficAccountingState.phase == 'ready',
                TrafficAccountingState.activation_requested.is_(True),
                exists(
                    select(TrafficBaseGrantIntent.id).where(
                        TrafficBaseGrantIntent.subscription_id == subscription.id,
                        TrafficBaseGrantIntent.state == 'pending',
                    )
                ),
            )
        )
        if state is None:
            raise AccountingConflict('Accounting requires a recorded panel account')
        recovered_identity = state.panel_identity
        try:
            panel_id = int(recovered_identity.split('@', 1)[0])
        except (ValueError, TypeError) as error:
            raise AccountingConflict('Invalid historical panel identity') from error
        if panel_id <= 0:
            raise AccountingConflict('Invalid historical panel identity')
    foreign = await find_foreign_panel_owner(
        db, subscription.user, subscription, panel_id, multi_tariff=settings.is_multi_tariff_enabled()
    )
    if foreign is not None:
        raise AccountingConflict('Panel account belongs to another subscription')
    panel = await api.get_user_by_id(panel_id)
    if panel is None:
        raise AccountingAccountMissing('Recorded panel account no longer exists')
    if panel.id != panel_id:
        raise AccountingConflict('Panel account identity changed')
    counters = read_counters(panel)
    if recovered_identity is not None:
        if counters.identity != recovered_identity:
            raise AccountingConflict('Historical panel epoch no longer matches')
        from app.services.panel_sync.writer import _record_identity

        await _record_identity(
            db, subscription.user, subscription, panel, multi_tariff=settings.is_multi_tariff_enabled()
        )
    return panel


async def ensure_managed_account(session_factory, api, subscription_id: int, *, now=None, timezone='UTC'):
    """Durable, resumable preflight BEFORE opening the money transaction.

    Commits only sessions owned here. The prepared fence commits before changing
    the panel; an ambiguous PATCH outcome remains resumable and never rolls back
    to autonomous resets. Existing writers must honor the subscription fence.
    """
    from zoneinfo import ZoneInfo

    from sqlalchemy import select

    from app.database.models import TrafficPurchase
    from app.services.panel_sync.writer import patch_managed_traffic
    from app.services.traffic_accounting import Allowance, migrate_preserving_available
    from app.services.traffic_accounting_schedule import next_base_refresh
    from app.services.traffic_accounting_store import _aware, initialize_account, load_account, save_account

    moment = now or datetime.now(UTC)
    calendar = ZoneInfo(timezone)
    async with session_factory() as db, db.begin():
        subscription, grace_open = await lock_accounting_subscription(db, subscription_id)
        loaded = await load_account(db, subscription_id)
        if loaded is not None and loaded[1].phase in {'ready', 'unlimited'}:
            return
        await _check_migration_preconditions(db, subscription, grace_open)
        if loaded is None:
            panel = await _fresh_panel(db, api, subscription)
            counters = read_counters(panel)
            strategy = getattr(panel.traffic_limit_strategy, 'value', panel.traffic_limit_strategy)
            next_refresh = next_base_refresh(strategy, now=moment, anchor=panel.created_at, timezone=calendar)
            advertised = getattr(panel, 'next_traffic_reset_at', None)
            if advertised is not None and (
                next_refresh is None or abs((_aware(advertised) - next_refresh).total_seconds()) > 60
            ):
                raise AccountingConflict('Panel traffic calendar disagrees with configured timezone')
            await initialize_account(
                db,
                subscription_id,
                TrafficAccount(counters.identity, counters.lifetime_bytes, counters.used_bytes, ()),
                strategy=strategy,
                timezone=timezone,
                anchor=panel.created_at,
                next_refresh_at=next_refresh,
            )
            subscription.traffic_accounting_mode = 'migrating'

    # A separate transaction makes the fence survive failed or timed-out writes.
    async with session_factory() as db, db.begin():
        subscription, grace_open = await lock_accounting_subscription(db, subscription_id)
        await _check_migration_preconditions(db, subscription, grace_open)
        account, state = await load_account(db, subscription_id)
        if state.phase == 'ready':
            return
        panel = await _fresh_panel(db, api, subscription)
        counters = read_counters(panel)
        if counters.identity != account.panel_identity:
            raise AccountingConflict('Panel account changed during migration')
        await patch_managed_traffic(api, user_id=panel.id)
        panel = await _fresh_panel(db, api, subscription)
        if getattr(panel.traffic_limit_strategy, 'value', panel.traffic_limit_strategy) != 'NO_RESET':
            raise AccountingConflict('Panel did not confirm NO_RESET')
        counters = read_counters(panel)
        if counters.identity != account.panel_identity:
            raise AccountingConflict('Panel account changed during migration')
        # A prepared transition may have survived an outage across a boundary.
        # Bootstrap still preserves ONLY the now-confirmed available bytes; it
        # does not sum missed calendar grants or retain an expired carry date.
        next_refresh = next_base_refresh(
            state.business_strategy,
            now=moment,
            anchor=_aware(state.calendar_anchor),
            timezone=ZoneInfo(state.calendar_timezone),
        )
        expiry = next_refresh or datetime.max.replace(tzinfo=UTC)
        # Subscription dates govern access separately; retaining quota after
        # access expires is needed for renewals which do not purchase a reset.
        purchases = (
            await db.scalars(
                select(TrafficPurchase).where(
                    TrafficPurchase.subscription_id == subscription_id, TrafficPurchase.expires_at > moment
                )
            )
        ).all()
        # purchased_traffic_gb may still include expired rows awaiting cleanup;
        # subtracting only active purchases would turn expired quota into base.
        base_gb = _base_bytes(subscription) // 1024**3
        grants = (
            Allowance('base:migration', base_gb * 1024**3, expiry),
            *(Allowance(f'purchase:{p.id}', p.traffic_gb * 1024**3, _aware(p.expires_at)) for p in purchases),
        )
        migrated = migrate_preserving_available(
            panel_identity=counters.identity,
            lifetime_bytes=counters.lifetime_bytes,
            used_bytes=counters.used_bytes,
            panel_limit_bytes=panel.traffic_limit_bytes,
            allowances=grants,
            now=moment,
            carry_expires_at=expiry,
        )
        await save_account(db, subscription_id, migrated, expected_revision=state.revision)
        state.phase = 'ready'
        state.next_refresh_at = next_refresh
        state.base_budget_bytes = base_gb * 1024**3
        state.last_base_operation_at = moment
        subscription.traffic_accounting_mode = 'managed'
        panel_status = getattr(getattr(panel, 'status', None), 'value', getattr(panel, 'status', None))
        if panel_status == 'DISABLED':
            subscription.status = 'disabled'
        state.quota_disabled = migrated.remaining_bytes(moment) == 0 and subscription.status != 'disabled'
        await db.flush()


def _require_lease(db, subscription_id):
    transaction = db.sync_session.get_transaction()
    if transaction is None or db.info.get('traffic_accounting_leases', {}).get(subscription_id) is not transaction:
        raise AccountingConflict('Accounting requires the current User/grace/Subscription lease')


def _base_bytes(subscription):
    return max(0, (subscription.traffic_limit_gb or 0) - (subscription.purchased_traffic_gb or 0)) * 1024**3


async def reconcile_account_locked(db, subscription, panel, *, now=None, refresh_calendar=True):
    """Settle a fresh GET; expire grants; issue at most one current calendar base.

    The caller obtains lock_accounting_subscription before its GET and retains
    that transaction through grant/projection. Never feed webhook event counters
    or a bulk snapshot fetched before the lock to this function.
    """
    from zoneinfo import ZoneInfo

    from app.services.traffic_accounting import Allowance, refresh_base
    from app.services.traffic_accounting_schedule import next_base_refresh
    from app.services.traffic_accounting_store import _aware, load_account, save_account

    _require_lease(db, subscription.id)
    if refresh_calendar:
        from sqlalchemy import select

        from app.database.models import TrafficBaseGrantIntent

        pending = await db.scalar(
            select(TrafficBaseGrantIntent.id)
            .where(TrafficBaseGrantIntent.subscription_id == subscription.id, TrafficBaseGrantIntent.state == 'pending')
            .limit(1)
        )
        if pending is not None:
            # A paid DAY→MONTH intent must replace the old calendar before an
            # overdue DAY event can supersede it. The consumer first settles
            # with refresh_calendar=False and flushes intents before returning.
            from app.services.traffic_base_grant_service import apply_pending_base_grants_locked

            return await apply_pending_base_grants_locked(db, subscription, panel, now=now)
    loaded = await load_account(db, subscription.id)
    if loaded is None or loaded[1].phase not in {'ready', 'unlimited'}:
        raise AccountingConflict('Accounting migration has not completed')
    account, state = loaded
    moment = now or datetime.now(UTC)
    account = settle_snapshot(
        account, panel, now=moment, exclude_usage=state.grace_excluded or state.phase == 'unlimited'
    )
    upcoming = None
    if (
        refresh_calendar
        and state.phase == 'ready'
        and not state.grace_excluded
        and state.next_refresh_at
        and _aware(state.next_refresh_at) <= moment
    ):
        upcoming = next_base_refresh(
            state.business_strategy,
            now=moment,
            anchor=_aware(state.calendar_anchor),
            timezone=ZoneInfo(state.calendar_timezone),
        )
        if upcoming is None:
            raise AccountingConflict('Scheduled base refresh has no calendar boundary')
        account = refresh_base(
            account, Allowance(f'base:calendar:{upcoming.isoformat()}', _base_bytes(subscription), upcoming)
        )
    await save_account(db, subscription.id, account, expected_revision=state.revision)
    if upcoming is not None:
        state.next_refresh_at = upcoming
        state.base_budget_bytes = _base_bytes(subscription)
        state.last_base_operation_at = moment
    await db.flush()
    return account


async def grant_purchase_locked(db, subscription, purchase, panel, *, now=None):
    """Charge and purchase must commit in the same caller-owned transaction."""
    from app.services.traffic_accounting import Allowance
    from app.services.traffic_accounting_store import _aware, load_account, save_account

    if purchase.subscription_id != subscription.id or not purchase.id or purchase.traffic_gb <= 0:
        raise AccountingConflict('Invalid traffic purchase')
    account = await reconcile_account_locked(db, subscription, panel, now=now)
    account = account.add(
        Allowance(f'purchase:{purchase.id}', purchase.traffic_gb * 1024**3, _aware(purchase.expires_at))
    )
    _, state = await load_account(db, subscription.id)
    await save_account(db, subscription.id, account, expected_revision=state.revision)
    return account


async def revoke_purchase_locked(db, subscription, purchase_id, panel, *, now=None):
    """Revoke only the selected package remainder, retaining its tombstone."""
    from app.services.traffic_accounting_store import load_account, save_account

    account = await reconcile_account_locked(db, subscription, panel, now=now)
    key = f'purchase:{purchase_id}'
    account = replace(
        account,
        allowances=tuple(replace(item, remaining_bytes=0) if item.key == key else item for item in account.allowances),
    )
    _, state = await load_account(db, subscription.id)
    await save_account(db, subscription.id, account, expected_revision=state.revision)
    schedule_projection_after_commit(db, subscription.id)
    return account


async def refresh_paid_base_locked(db, subscription, panel, *, operation_key: str, now=None):
    """Explicit paid/admin reset replaces only base, never purchased packages."""
    from app.services.traffic_accounting import Allowance, refresh_base
    from app.services.traffic_accounting_store import _aware, load_account, save_account

    if not operation_key or len(operation_key) > 150:
        raise AccountingConflict('A durable base refresh operation key is required')
    account = await reconcile_account_locked(db, subscription, panel, now=now)
    _, state = await load_account(db, subscription.id)
    if any(item.key == f'base:{operation_key}' for item in account.allowances):
        return account
    expiry = _aware(state.next_refresh_at) if state.next_refresh_at else datetime.max.replace(tzinfo=UTC)
    account = refresh_base(account, Allowance(f'base:{operation_key}', _base_bytes(subscription), expiry))
    await save_account(db, subscription.id, account, expected_revision=state.revision)
    state.base_budget_bytes = _base_bytes(subscription)
    state.last_base_operation_at = now or datetime.now(UTC)
    await db.flush()
    return account


async def transition_to_unlimited_locked(db, subscription, panel, *, now=None, refresh_calendar=True):
    """Explicit purchase of unlimited quota retires balances, retaining history."""
    from app.services.traffic_accounting_store import load_account, save_account

    account = await reconcile_account_locked(db, subscription, panel, now=now, refresh_calendar=refresh_calendar)
    retired = replace(account, allowances=tuple(replace(item, remaining_bytes=0) for item in account.allowances))
    _, state = await load_account(db, subscription.id)
    await save_account(db, subscription.id, retired, expected_revision=state.revision)
    state.phase = 'unlimited'
    state.base_budget_bytes = 0
    state.last_base_operation_at = now or datetime.now(UTC)
    state.next_refresh_at = None
    state.quota_disabled = False
    subscription.traffic_limit_gb = 0
    subscription.purchased_traffic_gb = 0
    subscription.traffic_reset_at = None
    await db.flush()


async def refresh_managed_account(db, subscription, *, operation_key=None, now=None):
    """Fresh locked panel observation and projection; caller owns commit."""
    from app.services.subscription_service import SubscriptionService
    from app.services.traffic_accounting_store import load_account

    try:
        _require_lease(db, subscription.id)
    except AccountingConflict:
        subscription, _ = await lock_accounting_subscription(db, subscription.id)
    async with SubscriptionService().get_api_client() as api:
        panel = await _fresh_panel(db, api, subscription)
        if operation_key is None:
            account = await reconcile_account_locked(db, subscription, panel, now=now)
        else:
            account = await refresh_paid_base_locked(db, subscription, panel, operation_key=operation_key, now=now)
        _, state = await load_account(db, subscription.id)
        await db.flush()
        schedule_projection_after_commit(db, subscription.id)
        return account


async def change_managed_tariff_locked(db, subscription, panel, *, operation_key, reset_base, now=None):
    """Admin switch retires old packages; resize preserves already spent base."""
    from app.services.traffic_accounting import Allowance, refresh_base
    from app.services.traffic_accounting_store import _aware, load_account, save_account

    moment = now or datetime.now(UTC)
    account = await reconcile_account_locked(db, subscription, panel, now=moment)
    _, state = await load_account(db, subscription.id)
    key = f'base:tariff:{operation_key}'
    if any(a.key == key for a in account.allowances):
        return account
    if subscription.traffic_limit_gb == 0:
        await transition_to_unlimited_locked(db, subscription, panel, now=moment)
        from app.services.traffic_accounting_sync import stage_managed_sync

        await stage_managed_sync(db, subscription)
        return (await load_account(db, subscription.id))[0]
    base_remaining = sum(
        a.remaining_bytes for a in account.allowances if a.key.startswith('base:') and a.expires_at > moment
    )
    consumed = max(0, state.base_budget_bytes - base_remaining)
    nominal = _base_bytes(subscription)
    amount = nominal if reset_base else max(0, nominal - consumed)
    retired = replace(account, allowances=tuple(replace(a, remaining_bytes=0) for a in account.allowances))
    from zoneinfo import ZoneInfo

    from app.services.panel_sync.traffic_strategy import get_traffic_reset_strategy
    from app.services.traffic_accounting_schedule import next_base_refresh

    target_strategy = get_traffic_reset_strategy(subscription.tariff)
    target_strategy = getattr(target_strategy, 'value', target_strategy)
    next_refresh = state.next_refresh_at
    if target_strategy != state.business_strategy or state.phase == 'unlimited':
        next_refresh = next_base_refresh(
            target_strategy,
            now=moment,
            anchor=_aware(state.calendar_anchor),
            timezone=ZoneInfo(state.calendar_timezone),
        )
    expiry = _aware(next_refresh) if next_refresh else datetime.max.replace(tzinfo=UTC)
    account = refresh_base(retired, Allowance(key, amount, expiry))
    await save_account(db, subscription.id, account, expected_revision=state.revision)
    state.base_budget_bytes = nominal
    state.last_base_operation_at = moment
    state.business_strategy = target_strategy
    state.next_refresh_at = next_refresh
    state.phase = 'ready'
    from app.services.traffic_accounting_sync import stage_managed_sync

    await stage_managed_sync(db, subscription)
    await db.flush()
    return account


_projection_tasks = set()
_MAX_IMMEDIATE_PROJECTIONS = 16


async def _deliver_projection_by_id(subscription_id):
    import structlog

    from app.database.database import AsyncSessionLocal
    from app.services.subscription_service import SubscriptionService
    from app.services.traffic_accounting_delivery import deliver_account_projection

    try:
        async with SubscriptionService().get_api_client() as api:
            await deliver_account_projection(AsyncSessionLocal, api, subscription_id)
    except Exception as error:
        structlog.get_logger(__name__).warning(
            'Traffic projection remains queued', subscription_id=subscription_id, error_type=type(error).__name__
        )


def schedule_projection_after_commit(db, subscription_id):
    """Bounded immediate delivery; outer rollback discards in-memory callbacks."""
    import asyncio

    from sqlalchemy import event

    pending = db.info.setdefault('traffic_projection_callbacks', set())
    pending.add(subscription_id)
    if db.info.get('traffic_projection_listeners'):
        return
    db.info['traffic_projection_listeners'] = True

    def committed(session):
        if session.in_nested_transaction():
            return
        ids = tuple(sorted(pending))
        pending.clear()
        for target_id in ids:
            if len(_projection_tasks) >= _MAX_IMMEDIATE_PROJECTIONS:
                break  # The persisted revision is recovered by the worker.
            task = asyncio.create_task(_deliver_projection_by_id(target_id))
            _projection_tasks.add(task)
            task.add_done_callback(_projection_tasks.discard)

    def rolled_back(_session):
        pending.clear()

    event.listen(db.sync_session, 'after_commit', committed)
    event.listen(db.sync_session, 'after_rollback', rolled_back)


async def stop_projection_tasks(timeout=5):
    """Drain then cancel immediate deliveries before grace/database shutdown."""
    import asyncio

    tasks = tuple(_projection_tasks)
    if not tasks:
        return
    _done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _projection_tasks.difference_update(tasks)


async def prepare_grace_accounting(db, subscription_id, *, now=None):
    """Included in grace's durable PENDING/RESTORING checkpoint transaction."""
    from sqlalchemy import select

    from app.database.models import Subscription
    from app.services.subscription_service import SubscriptionService
    from app.services.traffic_accounting_store import load_account, save_account

    mode = await db.scalar(select(Subscription.traffic_accounting_mode).where(Subscription.id == subscription_id))
    if mode != 'managed':
        return
    subscription, _ = await lock_accounting_subscription(db, subscription_id)
    async with SubscriptionService().get_api_client() as api:
        try:
            panel = await _fresh_panel(db, api, subscription)
        except AccountingAccountMissing:
            account, state = await load_account(db, subscription_id)
            if not state.grace_excluded:
                raise
            account = observe_usage(
                account, account.panel_identity, account.lifetime_bytes, account.used_bytes, now or datetime.now(UTC)
            )
            await save_account(db, subscription_id, account, expected_revision=state.revision)
        else:
            account = await reconcile_account_locked(db, subscription, panel, now=now)
    _, state = await load_account(db, subscription_id)
    state.grace_excluded = True
    if state.phase == 'ready' and account.remaining_bytes(now or datetime.now(UTC)) == 0:
        state.quota_disabled = True
    await db.flush()


async def finish_grace_accounting(db, subscription_id, *, now=None):
    """Advance the watermark through confirmed grace exit; never debit overlay."""
    from sqlalchemy import select

    from app.database.models import Subscription
    from app.services.subscription_service import SubscriptionService
    from app.services.traffic_accounting_store import load_account, save_account

    mode = await db.scalar(select(Subscription.traffic_accounting_mode).where(Subscription.id == subscription_id))
    if mode != 'managed':
        return
    subscription, _ = await lock_accounting_subscription(db, subscription_id)
    async with SubscriptionService().get_api_client() as api:
        try:
            panel = await _fresh_panel(db, api, subscription)
        except AccountingAccountMissing:
            # A confirmed deleted account has no remaining overlay to charge.
            # Close the exclusion using the last confirmed paid watermark;
            # recreation may then preserve that balance under its own intent.
            account, state = await load_account(db, subscription_id)
            account = observe_usage(
                account, account.panel_identity, account.lifetime_bytes, account.used_bytes, now or datetime.now(UTC)
            )
            await save_account(db, subscription_id, account, expected_revision=state.revision)
        else:
            await reconcile_account_locked(db, subscription, panel, now=now)
    _, state = await load_account(db, subscription_id)
    state.grace_excluded = False
    await db.flush()
    schedule_projection_after_commit(db, subscription_id)


async def restore_managed_grace(db, subscription_id, api, *, now=None):
    """Grace gateway restores current paid projection, never its old raw ceiling."""
    from sqlalchemy import select

    from app.config import settings
    from app.database.models import Subscription
    from app.services.panel_sync.payload import build_panel_payload, resolve_panel_status
    from app.services.panel_sync.writer import write_managed_payload_locked
    from app.services.traffic_accounting_store import load_account

    mode = await db.scalar(select(Subscription.traffic_accounting_mode).where(Subscription.id == subscription_id))
    if mode != 'managed':
        return None
    moment = now or datetime.now(UTC)
    subscription, _ = await lock_accounting_subscription(db, subscription_id)
    panel = await _fresh_panel(db, api, subscription)
    account = await reconcile_account_locked(db, subscription, panel, now=moment)
    _, state = await load_account(db, subscription_id)
    payload = build_panel_payload(
        subscription.user,
        subscription,
        multi_tariff=settings.is_multi_tariff_enabled(),
        now=moment,
        accounting_account=account,
    )
    if state.phase == 'unlimited':
        payload = replace(
            payload,
            traffic_limit_bytes=0,
            status=resolve_panel_status(subscription.user, subscription, is_live=payload.is_live, now=moment),
        )
    # PENDING/RESTORING checkpoint owns quota disabling before this PATCH.
    if state.phase == 'ready' and account.remaining_bytes(moment) == 0 and not state.quota_disabled:
        raise AccountingConflict('Grace restore needs a committed quota-disable checkpoint')
    return await write_managed_payload_locked(
        api, panel.id, payload, now=moment, panel_current=getattr(panel, 'expire_at', None)
    )


async def stage_authorized_activation(db, subscription):
    """Persist authorized admin activation or paid daily resume before commit."""
    from sqlalchemy import select

    from app.database.models import Subscription
    from app.services.traffic_accounting_store import load_account

    mode = await db.scalar(select(Subscription.traffic_accounting_mode).where(Subscription.id == subscription.id))
    if mode == 'legacy':
        return subscription
    try:
        _require_lease(db, subscription.id)
    except AccountingConflict:
        subscription, _ = await lock_accounting_subscription(db, subscription.id)
    await db.flush()
    loaded = await load_account(db, subscription.id)
    if loaded is None or loaded[1].phase not in {'ready', 'unlimited'}:
        raise AccountingConflict('Accounting is not ready for activation')
    state = loaded[1]
    state.activation_requested = True
    state.revision += 1
    state.next_delivery_at = None
    schedule_projection_after_commit(db, subscription.id)
    return subscription
