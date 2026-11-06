"""Entitlements are independent of the panel's resettable usage counter."""

from datetime import UTC, datetime, timedelta

import pytest

from app.services.traffic_accounting import (
    AccountingConflict,
    Allowance,
    TrafficAccount,
    observe_usage,
    refresh_base,
)


NOW = datetime(2026, 1, 1, tzinfo=UTC)
GB = 1024**3


def account(*allowances):
    return TrafficAccount('panel-1', 0, 0, tuple(allowances))


def grant(key, amount, days=30):
    return Allowance(key, amount * GB, NOW + timedelta(days=days))


def test_spent_expired_package_cannot_swallow_new_purchase():
    state = account(grant('base:jan', 40), grant('purchase:1', 40))
    state = observe_usage(state, 'panel-1', 80 * GB, 80 * GB, NOW + timedelta(days=1))
    state = state.add(grant('purchase:2', 40, days=60))
    assert state.remaining_bytes(NOW + timedelta(days=31)) == 40 * GB
    assert state.panel_limit_bytes(NOW + timedelta(days=31)) == 120 * GB


def test_reset_only_restores_base_and_keeps_package_remainder():
    state = account(grant('base:jan', 40), grant('purchase:1', 40, days=50), grant('purchase:2', 40, days=60))
    state = observe_usage(state, 'panel-1', 80 * GB, 80 * GB, NOW + timedelta(days=2))
    state = observe_usage(state, 'panel-1', 80 * GB, 0, NOW + timedelta(days=31))
    state = refresh_base(state, grant('base:feb', 40, days=59))
    assert state.remaining_bytes(NOW + timedelta(days=31)) == 80 * GB
    assert state.panel_limit_bytes(NOW + timedelta(days=31)) == 80 * GB


def test_repeated_sample_and_grants_are_idempotent():
    state = account(grant('base:jan', 40), grant('purchase:1', 40, days=50))
    state = observe_usage(state, 'panel-1', 50 * GB, 50 * GB, NOW + timedelta(days=1))
    assert observe_usage(state, 'panel-1', 50 * GB, 50 * GB, NOW + timedelta(days=1)) == state
    assert state.add(grant('purchase:1', 40, days=50)) == state
    assert refresh_base(state, grant('base:jan', 40)) == state


def test_earlier_expiry_is_consumed_first():
    state = account(grant('base:jan', 40), grant('purchase:1', 20, days=5))
    state = observe_usage(state, 'panel-1', 25 * GB, 25 * GB, NOW + timedelta(days=1))
    assert {item.key: item.remaining_bytes for item in state.allowances} == {'base:jan': 35 * GB, 'purchase:1': 0}


def test_expiry_only_removes_unused_remainder():
    state = account(grant('base:jan', 40), grant('purchase:1', 20, days=5))
    state = observe_usage(state, 'panel-1', 15 * GB, 15 * GB, NOW + timedelta(days=1))
    assert state.remaining_bytes(NOW + timedelta(days=6)) == 40 * GB
    assert state.panel_limit_bytes(NOW + timedelta(days=6)) == 55 * GB


@pytest.mark.parametrize(
    ('identity', 'lifetime', 'used'),
    [('panel-2', 10, 10), ('panel-1', 5, 5), ('panel-1', -1, 0), ('panel-1', 10, -1), ('panel-1', 10, 20)],
)
def test_invalid_counters_do_not_create_traffic(identity, lifetime, used):
    state = TrafficAccount('panel-1', 10, 10, (grant('base:jan', 40),))
    with pytest.raises(AccountingConflict):
        observe_usage(state, identity, lifetime, used, NOW)


def test_exhaustion_does_not_encode_unlimited():
    state = account(grant('base:jan', 0))
    assert state.remaining_bytes(NOW) == 0
    assert state.panel_limit_bytes(NOW) != 0


def test_usage_above_entitlement_does_not_consume_future_purchase():
    state = account(grant('base:jan', 40))
    state = observe_usage(state, 'panel-1', 45 * GB, 45 * GB, NOW)
    state = state.add(grant('purchase:1', 10))
    assert state.remaining_bytes(NOW) == 10 * GB


def test_same_operation_key_with_different_grant_is_a_conflict():
    state = account(grant('purchase:1', 40))
    with pytest.raises(AccountingConflict):
        state.add(grant('purchase:1', 80))


def test_old_period_replay_cannot_overwrite_current_base():
    state = account(grant('base:jan', 40))
    state = refresh_base(state, grant('base:feb', 40, days=59))
    assert refresh_base(state, grant('base:jan', 40)) == state


def test_counter_increase_without_lifetime_increase_is_rejected():
    state = TrafficAccount('panel-1', 100, 10, (grant('base:jan', 40),))
    with pytest.raises(AccountingConflict):
        observe_usage(state, 'panel-1', 100, 20, NOW)


@pytest.mark.parametrize(('limit', 'used'), [(80, 0), (80, 30), (80, 80), (40, 80), (120, 20)])
def test_migration_preserves_current_available_bytes(limit, used):
    from app.services.traffic_accounting import migrate_preserving_available

    migrated = migrate_preserving_available(
        panel_identity='panel-1',
        lifetime_bytes=200 * GB,
        used_bytes=used * GB,
        panel_limit_bytes=limit * GB,
        allowances=(grant('base:legacy', 40), grant('purchase:old', 40)),
        now=NOW,
        carry_expires_at=NOW + timedelta(days=30),
    )
    assert migrated.remaining_bytes(NOW) == max(0, limit - used) * GB
    assert migrated.lifetime_bytes == 200 * GB
    assert migrated.used_bytes == used * GB


def test_migration_does_not_restore_full_active_packages():
    from app.services.traffic_accounting import migrate_preserving_available

    migrated = migrate_preserving_available(
        panel_identity='panel-1',
        lifetime_bytes=80 * GB,
        used_bytes=80 * GB,
        panel_limit_bytes=80 * GB,
        allowances=(grant('base:legacy', 40), grant('purchase:old', 40)),
        now=NOW,
        carry_expires_at=NOW + timedelta(days=30),
    )
    assert migrated.remaining_bytes(NOW) == 0
    assert migrated.add(grant('purchase:new', 40)).remaining_bytes(NOW) == 40 * GB


def test_unlimited_account_cannot_be_migrated_as_empty_finite_quota():
    from app.services.traffic_accounting import migrate_preserving_available

    with pytest.raises(AccountingConflict):
        migrate_preserving_available(
            panel_identity='panel-1',
            lifetime_bytes=0,
            used_bytes=0,
            panel_limit_bytes=0,
            allowances=(),
            now=NOW,
            carry_expires_at=NOW + timedelta(days=1),
        )


def test_migration_rejects_reserved_carry_input():
    from app.services.traffic_accounting import migrate_preserving_available

    with pytest.raises(AccountingConflict):
        migrate_preserving_available(
            panel_identity='123',
            lifetime_bytes=0,
            used_bytes=0,
            panel_limit_bytes=20,
            allowances=(Allowance('legacy:carry', 10, NOW + timedelta(days=30)),),
            now=NOW,
            carry_expires_at=NOW + timedelta(days=30),
        )
