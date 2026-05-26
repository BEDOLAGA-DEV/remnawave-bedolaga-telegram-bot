"""Tests for convert_trial_to_paid_in_db: limits upgrade from tariff."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.crud.subscription import convert_trial_to_paid_in_db
from app.database.models import SubscriptionStatus


def _make_sub(**overrides):
    defaults = dict(
        id=42,
        user_id=7,
        is_trial=True,
        status=SubscriptionStatus.TRIAL.value,
        end_date=datetime.now(UTC) + timedelta(hours=4),
        traffic_limit_gb=5,
        device_limit=1,
        purchased_traffic_gb=0,
        traffic_used_gb=2.0,
        subscription_url='https://example/sub',
        tariff=None,
        updated_at=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _stub_session(sub):
    db = SimpleNamespace()
    scalar_result = SimpleNamespace(scalar_one_or_none=lambda: sub)
    db.execute = AsyncMock(return_value=scalar_result)
    db.flush = AsyncMock()
    return db


async def test_returns_none_when_subscription_missing():
    db = _stub_session(None)
    assert await convert_trial_to_paid_in_db(db, 999, period_days=30) is None


async def test_noop_when_already_paid():
    sub = _make_sub(is_trial=False, status=SubscriptionStatus.ACTIVE.value)
    db = _stub_session(sub)

    result = await convert_trial_to_paid_in_db(db, sub.id, period_days=30)

    assert result is sub
    db.flush.assert_not_awaited()
    assert sub.is_trial is False  # untouched


async def test_marks_paid_and_extends_end_date():
    end = datetime.now(UTC) + timedelta(hours=4)
    sub = _make_sub(end_date=end)
    db = _stub_session(sub)

    result = await convert_trial_to_paid_in_db(db, sub.id, period_days=30)

    assert result is sub
    assert sub.is_trial is False
    assert sub.status == SubscriptionStatus.ACTIVE.value
    # max(now, end_date) + 30d. end_date is in the future so it's the base.
    assert sub.end_date >= end + timedelta(days=30) - timedelta(seconds=2)
    assert sub.end_date <= end + timedelta(days=30) + timedelta(seconds=2)
    db.flush.assert_awaited_once()


async def test_end_date_uses_now_when_subscription_already_expired():
    end = datetime.now(UTC) - timedelta(hours=2)
    sub = _make_sub(end_date=end)
    db = _stub_session(sub)

    now_before = datetime.now(UTC)
    await convert_trial_to_paid_in_db(db, sub.id, period_days=30)

    # If sub already expired, base = now → new_end ≈ now + 30d (not end + 30d).
    expected_min = now_before + timedelta(days=30) - timedelta(seconds=2)
    assert sub.end_date >= expected_min


async def test_upgrades_limits_from_tariff():
    tariff = SimpleNamespace(id=11, traffic_limit_gb=200, device_limit=5)
    sub = _make_sub(tariff=tariff, traffic_limit_gb=5, device_limit=1)
    db = _stub_session(sub)

    await convert_trial_to_paid_in_db(db, sub.id, period_days=30)

    assert sub.traffic_limit_gb == 200
    assert sub.device_limit == 5


async def test_preserves_accumulated_state():
    tariff = SimpleNamespace(id=11, traffic_limit_gb=200, device_limit=5)
    sub = _make_sub(
        tariff=tariff,
        purchased_traffic_gb=15,
        traffic_used_gb=42.5,
        subscription_url='https://example/sub/abc',
    )
    db = _stub_session(sub)

    await convert_trial_to_paid_in_db(db, sub.id, period_days=30)

    # Accumulated usage / purchases / URL must NOT be reset on trial→paid.
    assert sub.purchased_traffic_gb == 15
    assert sub.traffic_used_gb == 42.5
    assert sub.subscription_url == 'https://example/sub/abc'


async def test_skips_limits_upgrade_when_no_tariff():
    sub = _make_sub(tariff=None, traffic_limit_gb=5, device_limit=1)
    db = _stub_session(sub)

    await convert_trial_to_paid_in_db(db, sub.id, period_days=30)

    # No tariff → keep original limits (e.g. campaign-issued trial).
    assert sub.traffic_limit_gb == 5
    assert sub.device_limit == 1
    assert sub.is_trial is False  # still flipped to paid


async def test_unlimited_tariff_limits_propagate():
    tariff = SimpleNamespace(id=11, traffic_limit_gb=0, device_limit=10)
    sub = _make_sub(tariff=tariff, traffic_limit_gb=5, device_limit=1)
    db = _stub_session(sub)

    await convert_trial_to_paid_in_db(db, sub.id, period_days=30)

    assert sub.traffic_limit_gb == 0  # 0 = безлимит, propagate as-is
    assert sub.device_limit == 10


@pytest.mark.parametrize('period_days', [7, 30, 90, 180, 365])
async def test_period_days_applied(period_days):
    end = datetime.now(UTC) + timedelta(hours=2)
    sub = _make_sub(end_date=end)
    db = _stub_session(sub)

    await convert_trial_to_paid_in_db(db, sub.id, period_days=period_days)

    expected = end + timedelta(days=period_days)
    diff = abs((sub.end_date - expected).total_seconds())
    assert diff < 5
