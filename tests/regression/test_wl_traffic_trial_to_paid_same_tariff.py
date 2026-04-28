"""Regression: extend_subscription must sync wl_traffic_limit_gb on every
extend, not only on tariff change / expired sub.

Before fix: the WL update was gated behind `is_tariff_change or was_expired`,
so a trial→paid conversion on the same tariff_id (TRIAL_TARIFF_ID) left the
subscription with the trial's default WL=5 even though the tariff says 15.
"""
import inspect
from app.database.crud import subscription as sub_crud


def test_extend_subscription_wl_update_not_gated_by_tariff_change():
    """Source-level guard: the WL-limit assignment must NOT be inside an
    `if … (is_tariff_change or was_expired):` block any longer.

    We check the function source for the post-fix invariant: the assignment
    `subscription.wl_traffic_limit_gb = wl_traffic_limit_gb` must appear
    inside `if wl_traffic_limit_gb is not None:` and the counter reset
    (`subscription.wl_traffic_used_gb = 0.0`) must be in a separately-gated
    branch.
    """
    src = inspect.getsource(sub_crud.extend_subscription)

    # Post-fix: limit update happens whenever wl_traffic_limit_gb is provided.
    assert 'if wl_traffic_limit_gb is not None:' in src, (
        'Expected unconditional WL limit sync block — pre-fix gate may be back.'
    )

    # Post-fix: the counter reset is in a separate `is_tariff_change or was_expired`
    # block, not the same one as the limit assignment.
    limit_assign_idx = src.index('subscription.wl_traffic_limit_gb = wl_traffic_limit_gb')
    counters_reset_idx = src.index('subscription.wl_traffic_used_gb = 0.0')
    gate_idx = src.index('is_tariff_change or was_expired')

    # The limit assignment should appear BEFORE the gated reset block.
    assert limit_assign_idx < gate_idx < counters_reset_idx, (
        'Expected order: assign limit → gate check → reset counters. '
        'Pre-fix code had assignment INSIDE the gate.'
    )
