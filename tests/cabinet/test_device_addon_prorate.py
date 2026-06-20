"""Tests for device-addon proration (prorated to full remaining period, no cap).

Background — Telegram bug reports #596757/#587412 vs the later "50₽ за год" report
----------------------------------------------------------------------------------
Admin sets `device_price_kopeks`, labelled "Цена за устройство (30 дней)" — a
*monthly* rate. When a user adds a device mid-subscription, the add-on is prorated
to the **actual remaining days** of the subscription, exactly like every other
add-on (traffic, servers, countries) via `calculate_prorated_price`.

History:
- Originally device add-ons prorated to the full remaining period (correct).
- #596757/#587412 introduced a one-month cap (`effective_days = min(days_left,
  30)`), assuming "renewal tops up the rest monthly". That assumption only holds
  for ~30-day tariffs: for a 366-day subscription the next renewal is 366 days
  away, so an extra device cost a flat one month (e.g. 50₽) for the *whole year* —
  while the renewal of that same subscription charges the device for all ~12
  months (`pricing_engine`: `extra_devices × price × months`). That asymmetry was
  reported again ("устройства стали на год 50₽ стоить").
- Fix: drop the cap. Add-on price = monthly_rate × remaining_days / 30, no upper
  bound, 1₽ floor — matching `calculate_prorated_price`. The device persists
  across renewals and is re-billed for each new period by `pricing_engine`.

Both the bot (`app/handlers/subscription/devices.py`, via
`calculate_prorated_price`) and the cabinet
(`app/cabinet/routes/subscription_modules/devices.py`, inline
`int(price × devices × days_left / 30)` with a 1₽ floor) must agree.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.utils.pricing_utils import calculate_prorated_price


def _bot_quote(monthly_kopeks: int, days_left: int) -> int:
    """Canonical proration used by the bot device handler (real production code)."""
    end_date = datetime.now(UTC) + timedelta(days=days_left)
    total, _ = calculate_prorated_price(monthly_kopeks, end_date)
    return total


def _cabinet_quote(device_price_kopeks: int, devices: int, days_left: int) -> int:
    """Mirror the cabinet inline formula in devices.py (no cap)."""
    base = int(device_price_kopeks * devices * days_left / 30)
    if devices > 0 and device_price_kopeks > 0:
        base = max(100, base)  # Минимум 1 рубль
    return base


@pytest.mark.parametrize(
    ('days_left', 'expected_kopeks'),
    [
        (5, 416),  # 25₽ × 5/30 ≈ 4.17₽ — prorated DOWN for a short remainder
        (12, 1000),  # 25₽ × 12/30 = 10₽
        (30, 2500),  # exactly one month = 25₽
        (58, 4833),  # ~2 months ≈ 48₽ (NOT capped at 25₽ anymore)
        (116, 9666),  # ~4 months ≈ 97₽ (the previously-"fixed" charge, now correct)
        (365, 30416),  # ~1 year ≈ 304₽
    ],
)
def test_device_addon_prorates_to_full_remaining_period(days_left: int, expected_kopeks: int) -> None:
    """Device add-on scales with the actual remaining days — no one-month cap."""
    assert _bot_quote(2500, days_left) == expected_kopeks


def test_no_upper_cap_long_subscription_costs_more_than_one_month() -> None:
    """A year-long subscription must charge ~12× the monthly rate, not a flat month.

    This is the regression being fixed: a 50₽/month device on a 366-day sub costs
    ~610₽, not 50₽.
    """
    monthly = 5000  # 50₽
    one_month = _bot_quote(monthly, 30)
    full_year = _bot_quote(monthly, 366)
    assert one_month == 5000
    assert full_year == 61000  # 50₽ × 366/30
    assert full_year > one_month * 11  # clearly scales with the remaining duration


def test_short_remainder_is_prorated_down() -> None:
    """5 days left → pay for ~5 days, not a full month."""
    assert _bot_quote(5000, 5) == int(5000 * 5 / 30)  # ≈ 833 kopeks
    assert _bot_quote(5000, 5) < _bot_quote(5000, 30)


def test_one_ruble_floor_for_paid_devices() -> None:
    """Tiny prorated amounts floor to 1₽ (100 kopeks)."""
    assert _bot_quote(100, 1) == 100  # 100 × 1/30 = 3 → floored to 100


def test_free_devices_cost_nothing() -> None:
    """Zero chargeable monthly price → free, no floor applied."""
    assert _bot_quote(0, 366) == 0


def test_multi_device_scales_linearly() -> None:
    """N devices = N × per-device prorated price."""
    assert _cabinet_quote(2500, 2, 60) == 10000  # 25₽ × 2 × 60/30 = 100₽
    assert _cabinet_quote(2500, 1, 60) == 5000  # 25₽ × 1 × 60/30 = 50₽


def test_bot_and_cabinet_formulas_agree() -> None:
    """The bot (calculate_prorated_price) and the cabinet inline math must match."""
    for days in (5, 12, 30, 58, 116, 200, 365, 366):
        assert _bot_quote(2500, days) == _cabinet_quote(2500, 1, days), f'mismatch at {days} days'
