from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.paid_traffic_reset_service import _valid


def test_period_reset_uses_paid_end_date_not_daily_billing_clock():
    end = datetime.now(UTC) + timedelta(days=30)
    user = SimpleNamespace(status='active')
    sub = SimpleNamespace(
        user_id=1,
        status='active',
        is_trial=False,
        is_daily_paused=False,
        grace_session_open=False,
        end_date=end,
        last_daily_charge_at=None,
    )
    intent = SimpleNamespace(user_id=1, charge_at=end, operation_key='cispay:payment:123')
    assert _valid(user, sub, intent)
    sub.end_date += timedelta(days=1)
    assert not _valid(user, sub, intent)


def test_daily_reset_still_requires_daily_charge_clock():
    end = datetime.now(UTC) + timedelta(days=30)
    user = SimpleNamespace(status='active')
    sub = SimpleNamespace(
        user_id=1,
        status='active',
        is_trial=False,
        is_daily_paused=False,
        grace_session_open=False,
        end_date=end,
        last_daily_charge_at=None,
    )
    intent = SimpleNamespace(user_id=1, charge_at=end, operation_key='daily:1:123')
    assert not _valid(user, sub, intent)
