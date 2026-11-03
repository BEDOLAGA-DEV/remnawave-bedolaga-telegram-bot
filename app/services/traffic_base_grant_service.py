"""Persist paid base renewals before commit; recover grants without replaying money."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.database.models import TrafficAccountingState, TrafficBaseGrantIntent
from app.services.traffic_accounting import AccountingConflict, Allowance, refresh_base
from app.services.traffic_accounting_schedule import next_base_refresh
from app.services.traffic_accounting_store import _aware, load_account, save_account


async def stage_paid_base_grant(db, subscription, *, operation_key: str, reset_base: bool = True):
    """Called only for an explicit paid/reset decision, inside its money transaction.

    No panel requests, grants or commits. User/subscription serialization belongs
    to the financial caller; the accounting worker takes its lease at delivery.
    """
    mode = getattr(subscription, 'traffic_accounting_mode', None)
    if mode == 'migrating':
        raise AccountingConflict('Paid grant awaits completed accounting migration')
    if mode != 'managed':
        return None
    if not operation_key or len(operation_key) > 128:
        raise AccountingConflict('A durable paid grant operation key is required')
    # Financial sessions may disable autoflush. Preserve their staged phase and
    # expose an earlier same-transaction intent before querying idempotency.
    await db.flush()
    existing = await db.scalar(
        select(TrafficBaseGrantIntent).where(
            TrafficBaseGrantIntent.subscription_id == subscription.id,
            TrafficBaseGrantIntent.operation_key == operation_key,
        )
    )
    if existing is not None:
        return existing
    state = await db.scalar(
        select(TrafficAccountingState)
        .where(TrafficAccountingState.subscription_id == subscription.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if state is None or state.phase not in ('ready', 'unlimited', 'suspended'):
        raise AccountingConflict('Paid grant awaits completed accounting migration')
    base = max(0, (subscription.traffic_limit_gb or 0) - (subscription.purchased_traffic_gb or 0)) * 1024**3
    from app.database.models import Tariff
    from app.services.panel_sync.traffic_strategy import get_traffic_reset_strategy

    tariff = await db.get(Tariff, subscription.tariff_id) if subscription.tariff_id else None
    strategy = get_traffic_reset_strategy(tariff).value
    moment = datetime.now(UTC)
    expiry = next_base_refresh(
        strategy, now=moment, anchor=_aware(state.calendar_anchor), timezone=ZoneInfo(state.calendar_timezone)
    )
    intent = TrafficBaseGrantIntent(
        subscription_id=subscription.id,
        operation_key=operation_key,
        base_bytes=base,
        reset_base=reset_base,
        target_unlimited=subscription.traffic_limit_gb == 0,
        business_strategy=strategy,
        tariff_id=subscription.tariff_id,
        requested_at=moment,
        expires_at=(
            datetime.max.replace(tzinfo=UTC)
            if subscription.traffic_limit_gb == 0
            else expiry or datetime.max.replace(tzinfo=UTC)
        ),
        state='pending',
    )
    db.add(intent)
    if state.phase == 'suspended':
        # A new authorized entitlement re-arms recovery in the SAME transaction
        # as its payment. Cleanup tombstones remain; rollback leaves suspension
        # intact, and a later cleanup supersedes this intent again.
        state.phase = 'ready'
        state.activation_requested = True
    # A new paid intent is durable work even before its allowance is applied.
    # Advance the worker CAS token so an older delivery result cannot replace
    # this wake-up with its stale backoff (including None -> None races).
    from app.services.traffic_accounting_sync import stage_managed_sync

    await stage_managed_sync(db, subscription)
    await db.flush()
    return intent


async def apply_pending_base_grants_locked(db, subscription, panel, *, now=None):
    """Fresh usage first; never resurrect an old grant after a newer base operation."""
    from app.services.traffic_accounting_service import reconcile_account_locked, transition_to_unlimited_locked

    moment = now or datetime.now(UTC)
    account = await reconcile_account_locked(db, subscription, panel, now=moment, refresh_calendar=False)
    intents = list(
        (
            await db.scalars(
                select(TrafficBaseGrantIntent)
                .where(
                    TrafficBaseGrantIntent.subscription_id == subscription.id,
                    TrafficBaseGrantIntent.state == 'pending',
                )
                .order_by(TrafficBaseGrantIntent.id)
                .with_for_update()
            )
        ).all()
    )
    for intent in intents:
        account, state = await load_account(db, subscription.id)
        current_unlimited = subscription.traffic_limit_gb == 0
        current_base = max(0, (subscription.traffic_limit_gb or 0) - (subscription.purchased_traffic_gb or 0)) * 1024**3
        newer_operation = state.last_base_operation_at is not None and _aware(intent.requested_at) <= _aware(
            state.last_base_operation_at
        )
        if (
            intent.tariff_id != subscription.tariff_id
            or intent.target_unlimited != current_unlimited
            or (not current_unlimited and intent.base_bytes != current_base)
            or state.phase not in ('ready', 'unlimited')
            or newer_operation
        ):
            intent.state = 'superseded'
        elif not intent.target_unlimited and _aware(intent.expires_at) <= moment:
            # The paid period has elapsed. Keep its authoritative calendar, then
            # let reconciliation issue only today's base instead of reviving it.
            state.business_strategy = intent.business_strategy
            state.next_refresh_at = intent.expires_at
            state.phase = 'ready'
            state.base_budget_bytes = intent.base_bytes
            state.last_base_operation_at = intent.requested_at
            intent.state = 'superseded'
        elif intent.target_unlimited:
            await transition_to_unlimited_locked(db, subscription, panel, now=moment, refresh_calendar=False)
            account, state = await load_account(db, subscription.id)
            state.last_base_operation_at = intent.requested_at
            intent.state = 'applied'
        else:
            key = f'base:intent:{intent.id}'
            base_remaining = sum(
                a.remaining_bytes for a in account.allowances if a.key.startswith('base:') and a.expires_at > moment
            )
            consumed = max(0, state.base_budget_bytes - base_remaining)
            remaining = intent.base_bytes if intent.reset_base else max(0, intent.base_bytes - consumed)
            account = refresh_base(account, Allowance(key, remaining, _aware(intent.expires_at), intent.base_bytes))
            await save_account(db, subscription.id, account, expected_revision=state.revision)
            state.base_budget_bytes = intent.base_bytes
            state.phase = 'ready'
            state.business_strategy = intent.business_strategy
            state.next_refresh_at = None if intent.business_strategy == 'NO_RESET' else intent.expires_at
            state.last_base_operation_at = intent.requested_at
            intent.state = 'applied'
        intent.processed_at = moment
        await db.flush()
    return await reconcile_account_locked(db, subscription, panel, now=moment)
