"""Guard generic enables and checkpoint usage before intentional panel deletion."""

from datetime import UTC, datetime

from sqlalchemy import select

from app.database.models import TrafficAccountingState
from app.services.traffic_accounting import AccountingConflict
from app.services.traffic_accounting_service import (
    AccountingAccountMissing,
    _fresh_panel,
    lock_accounting_subscription,
    reconcile_account_locked,
    schedule_projection_after_commit,
    settle_snapshot,
)
from app.services.traffic_accounting_store import _aware, load_account, save_account


async def _account_id(db, panel_id):
    result = await db.scalar(
        select(TrafficAccountingState.subscription_id)
        .where(TrafficAccountingState.panel_identity.like(f'{int(panel_id)}@%'))
        .limit(1)
    )
    return result if isinstance(result, int) else None


async def try_enable_managed_account(db, api, panel_id, *, now=None):
    """Return (handled, confirmed_active); unknown quota never falls to enable."""
    subscription_id = await _account_id(db, panel_id)
    if subscription_id is None:
        return False, False
    moment = now or datetime.now(UTC)
    subscription, grace_open = await lock_accounting_subscription(db, subscription_id)
    loaded = await load_account(db, subscription_id)
    if loaded is None or loaded[1].phase not in {'ready', 'unlimited'} or grace_open or loaded[1].grace_excluded:
        return True, False
    panel = await _fresh_panel(db, api, subscription)
    account = await reconcile_account_locked(db, subscription, panel, now=moment)
    _, state = await load_account(db, subscription_id)
    schedule_projection_after_commit(db, subscription_id)
    panel_status = getattr(getattr(panel, 'status', None), 'value', getattr(panel, 'status', None))
    if panel_status == 'DISABLED' and not state.quota_disabled:
        subscription.status = 'disabled'
        return True, False
    available = state.phase == 'unlimited' or account.remaining_bytes(moment) > 0
    if not available or subscription.user.status != 'active' or _aware(subscription.end_date) <= moment:
        return True, False
    if subscription.status == 'limited' and state.quota_disabled:
        subscription.status = 'active'
    if subscription.status not in {'active', 'trial'}:
        return True, False
    # Even enable can open the one-byte zero-quota sentinel. Defer any actual
    # change to the owned post-commit projector, so a caller rolling back its
    # purchase cannot open access. Report only already observed ACTIVE as such.
    return True, panel_status == 'ACTIVE'


async def checkpoint_managed_deletion(session_factory, api, panel_id, *, now=None):
    """Suspend recovery and commit the final fresh sample before DELETE.

    The caller owns the explicit delete operation; its ordinary transaction must
    release locks before entering these separately checkpointed sessions. An
    ambiguous DELETE leaves the account suspended, never automatically recreated.
    An explicit later create may resume it through the recreation protocol.
    """
    async with session_factory() as db, db.begin():
        subscription_id = await _account_id(db, panel_id)
        if subscription_id is None:
            return False
        subscription, grace_open = await lock_accounting_subscription(db, subscription_id)
        _, state = await load_account(db, subscription_id)
        if grace_open or state.grace_excluded:
            raise AccountingConflict('Cannot delete a panel account with open grace')
        if state.phase not in {'ready', 'unlimited', 'suspended'}:
            raise AccountingConflict('Finish the accounting transition before panel deletion')
        try:
            panel = await _fresh_panel(db, api, subscription)
        except AccountingAccountMissing:
            panel = None
        if panel is not None:
            if state.phase == 'suspended':
                account, state = await load_account(db, subscription_id)
                account = settle_snapshot(
                    account,
                    panel,
                    now=now or datetime.now(UTC),
                    exclude_usage=state.recreation_previous_phase == 'unlimited',
                )
                await save_account(db, subscription_id, account, expected_revision=state.revision)
            else:
                await reconcile_account_locked(db, subscription, panel, now=now)
                _, state = await load_account(db, subscription_id)
        if state.phase != 'suspended':
            state.recreation_previous_phase = state.phase
            state.phase = 'suspended'
        # The transaction commits both watermark and suspension before DELETE.
    return True


async def retire_managed_account(db, subscription_id, *, now=None):
    """Retire paid rights during the existing destructive subscription cleanup.

    Keep operation tombstones so neither retries nor account recreation can
    restore the discarded quota. The caller commits the cleanup atomically.
    """
    from dataclasses import replace

    from sqlalchemy import delete, update

    from app.database.models import TrafficBaseGrantIntent, TrafficPurchase

    subscription, grace_open = await lock_accounting_subscription(db, subscription_id)
    loaded = await load_account(db, subscription_id)
    if loaded is None:
        return subscription
    account, state = loaded
    if grace_open or state.grace_excluded:
        raise AccountingConflict('Finish the open grace incident before retiring traffic')
    retired = replace(account, allowances=tuple(replace(item, remaining_bytes=0) for item in account.allowances))
    await save_account(db, subscription_id, retired, expected_revision=state.revision)
    state.phase = 'suspended'
    state.recreation_previous_phase = 'ready'
    state.base_budget_bytes = 0
    state.next_refresh_at = None
    state.activation_requested = False
    state.quota_disabled = False
    state.pending_sync_fields = []
    state.pending_user_tag = None
    # Package rows are nominal purchase metadata, not the immutable accounting
    # history. Keeping them after full cleanup lets renewal housekeeping add
    # retired packages back to the next subscription's nominal limit/top-up cap.
    await db.execute(delete(TrafficPurchase).where(TrafficPurchase.subscription_id == subscription_id))
    subscription.purchased_traffic_gb = 0
    subscription.traffic_reset_at = None
    await db.execute(
        update(TrafficBaseGrantIntent)
        .where(TrafficBaseGrantIntent.subscription_id == subscription_id, TrafficBaseGrantIntent.state == 'pending')
        .values(state='superseded', processed_at=now or datetime.now(UTC))
    )
    return subscription
