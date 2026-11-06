"""Explicit account-loss recovery; preserves last confirmed balances and history."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from secrets import token_hex

from app.config import settings
from app.services.traffic_accounting import AccountingConflict, observe_usage
from app.services.traffic_accounting_service import lock_accounting_subscription, read_counters
from app.services.traffic_accounting_store import _aware, load_account, save_account


async def ensure_account_recreated(session_factory, api, subscription_id, *, now=None, allow_suspended=False):
    """Create disabled under a durable unique username; recover ambiguous POSTs.

    Final unobserved usage of a deleted account is unrecoverable. Preserve the
    last confirmed remainder, never a new base/package grant. Unknown identity
    changes are NOT routed here; the previous account must explicitly be absent.
    """
    from app.services.panel_sync.identity import find_foreign_panel_owner
    from app.services.panel_sync.writer import _record_identity, create_managed_replacement

    moment = now or datetime.now(UTC)
    async with session_factory() as db, db.begin():
        subscription, grace_open = await lock_accounting_subscription(db, subscription_id)
        loaded = await load_account(db, subscription_id)
        if loaded is None:
            raise AccountingConflict('Recreation requires an existing accounting ledger')
        account, state = loaded
        if grace_open or state.grace_excluded:
            raise AccountingConflict('Finish the open grace incident before account recreation')
        if state.phase == 'suspended' and not allow_suspended:
            raise AccountingConflict('Intentionally suspended account requires explicit creation')
        if state.phase != 'recreating':
            if state.phase not in {'ready', 'unlimited', 'suspended'}:
                raise AccountingConflict('Cannot recreate an incomplete migration')
            old_id = int(account.panel_identity.split('@', 1)[0])
            if await api.get_user_by_id(old_id) is not None:
                raise AccountingConflict('The previous panel account still exists')
            if state.phase != 'suspended':
                state.recreation_previous_phase = state.phase
            state.recreation_started_at = moment
            state.recreation_username = f'r_{token_hex(16)}'
            state.phase = 'recreating'
            subscription.traffic_accounting_mode = 'migrating'

    async with session_factory() as db, db.begin():
        subscription, grace_open = await lock_accounting_subscription(db, subscription_id)
        account, state = await load_account(db, subscription_id)
        if state.phase in {'ready', 'unlimited'}:
            return
        if state.phase != 'recreating' or grace_open or state.grace_excluded:
            raise AccountingConflict('Recreation state changed')
        old_id = int(account.panel_identity.split('@', 1)[0])
        if await api.get_user_by_id(old_id) is not None:
            raise AccountingConflict('The previous account reappeared; operator reconciliation is required')
        # Expire balances at today's boundary but preserve every old operation.
        settled = observe_usage(account, account.panel_identity, account.lifetime_bytes, account.used_bytes, moment)
        provisional = replace(settled, lifetime_bytes=0, used_bytes=0)
        replacement = await api.get_user_by_username(state.recreation_username)
        if replacement is None:
            await create_managed_replacement(
                api, subscription.user, subscription, provisional, username=state.recreation_username, now=moment
            )
            replacement = await api.get_user_by_username(state.recreation_username)
        if replacement is None or replacement.username != state.recreation_username:
            raise AccountingConflict('Replacement account was not confirmed')
        counters = read_counters(replacement)
        if replacement.created_at < _aware(state.recreation_started_at) - timedelta(minutes=5):
            raise AccountingConflict('Replacement predates the durable recreation intent')
        foreign = await find_foreign_panel_owner(
            db, subscription.user, subscription, replacement.id, multi_tariff=settings.is_multi_tariff_enabled()
        )
        if foreign is not None:
            raise AccountingConflict('Replacement belongs to another subscriber')
        rebased = observe_usage(
            replace(provisional, panel_identity=counters.identity),
            counters.identity,
            counters.lifetime_bytes,
            counters.used_bytes,
            moment,
        )
        # This is the only intentional epoch rebase. The phase and unique
        # creation intent were committed before POST; normal save rejects it.
        state.panel_identity = counters.identity
        state.lifetime_bytes = counters.lifetime_bytes
        state.used_bytes = counters.used_bytes
        await db.flush()
        await save_account(db, subscription_id, rebased, expected_revision=state.revision)
        state.phase = state.recreation_previous_phase
        state.quota_disabled = True  # replacement is intentionally born DISABLED
        state.projected_revision = -1
        subscription.traffic_accounting_mode = 'managed'
        await _record_identity(
            db,
            subscription.user,
            subscription,
            replacement,
            multi_tariff=settings.is_multi_tariff_enabled(),
            replace_stale=True,
            dead_panel_user_id=old_id,
        )
