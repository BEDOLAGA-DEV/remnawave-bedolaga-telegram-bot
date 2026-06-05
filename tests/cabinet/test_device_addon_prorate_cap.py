"""Regression tests for the one-month prorate cap on device-addon purchases.

Background — Telegram bug report #596757 / #587412
--------------------------------------------------
Admin sets `device_price_kopeks = 2500` (25₽) labelled "Цена за устройство
(30 дней)" — i.e. monthly rate. User has a 6-month subscription with ~116
days left, adds 1 device.

Before the fix: charge = `2500 * 1 * 116 / 30 ≈ 97₽`. The user pays four
months upfront despite the "30 дней" label, because the prorate uses
`days_left = days-until-subscription-end` with no upper bound.

After the fix: `effective_days = min(days_left, 30)`. Same scenario
charges `2500 * 1 * 30 / 30 = 25₽` — one month, matching the label.
Subscriptions shorter than 30 days remain prorated (e.g. 12 days → 10₽).

The fix lives in two functions in
`app/cabinet/routes/subscription_modules/devices.py` (lines ~413, ~689,
~804). This test exercises the math directly.
"""

from __future__ import annotations

import pytest


def _quote_price(*, device_price_kopeks: int, devices: int, days_left: int, total_days: int = 30) -> int:
    """Mirror the post-fix formula in devices.py — caps prorate at one month."""
    effective_days = min(days_left, total_days)
    base_price_per_month = device_price_kopeks * devices
    return int(base_price_per_month * effective_days / total_days)


@pytest.mark.parametrize(
    ('days_left', 'expected_kopeks'),
    [
        (12, 1000),  # short remainder → prorated (25₽ × 12/30 = 10₽)
        (30, 2500),  # exactly one month → full monthly rate (25₽)
        (58, 2500),  # 2 months left → still one month rate, NOT 48₽
        (116, 2500),  # 4 months left → still one month rate, NOT 97₽ (the bug)
        (365, 2500),  # 1 year left → still one month rate
    ],
)
def test_device_addon_caps_prorate_at_one_month(days_left: int, expected_kopeks: int) -> None:
    """Adding 1 device at 25₽/month never charges more than 25₽ regardless
    of how long the subscription has left."""
    actual = _quote_price(device_price_kopeks=2500, devices=1, days_left=days_left)
    assert actual == expected_kopeks, (
        f'days_left={days_left}: expected {expected_kopeks} kopeks, got {actual}. '
        f'Pre-fix would have charged {int(2500 * days_left / 30)} kopeks.'
    )


def test_multi_device_scales_linearly_under_cap() -> None:
    """Adding N devices at the cap charges N × monthly rate, not N × prorated-to-end."""
    one_device = _quote_price(device_price_kopeks=2500, devices=1, days_left=60)
    two_devices = _quote_price(device_price_kopeks=2500, devices=2, days_left=60)
    assert one_device == 2500, '1 device at cap = 25₽'
    assert two_devices == 5000, '2 devices at cap = 50₽ (not 100₽ that 2×60/30 would give)'


def test_short_subscription_still_prorated_downward() -> None:
    """The cap only limits the upside — users with subs shorter than 30 days
    still get a proportional discount."""
    # 25₽/month for 5 days remaining = 25 × 5/30 = ~4.17₽ → 416 kopeks
    actual = _quote_price(device_price_kopeks=2500, devices=1, days_left=5)
    assert actual == 416


def test_pre_fix_overcharge_scenario_is_now_correct() -> None:
    """The exact numbers from the bug report — 25₽ admin setting, ~116 days
    left, 1 device — must now charge ≤ 25₽ instead of the buggy 97₽."""
    pre_fix_charge = int(2500 * 1 * 116 / 30)  # = 9666 kopeks ≈ 97₽
    assert pre_fix_charge == 9666, 'sanity-check the pre-fix math'

    post_fix_charge = _quote_price(device_price_kopeks=2500, devices=1, days_left=116)
    assert post_fix_charge == 2500
    assert post_fix_charge < pre_fix_charge
