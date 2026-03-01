from datetime import UTC, datetime

import pytest

from app.database.crud.campaign import get_campaign_period_bounds
from app.utils.timezone import get_local_timezone


@pytest.fixture(autouse=True)
def force_utc_timezone(monkeypatch):
    monkeypatch.setenv('TZ', 'UTC')
    monkeypatch.setattr('app.config.settings.TIMEZONE', 'UTC', raising=False)
    get_local_timezone.cache_clear()
    yield
    get_local_timezone.cache_clear()


def test_campaign_period_bounds_day():
    now = datetime(2026, 3, 15, 13, 45, tzinfo=UTC)
    start_at, end_at = get_campaign_period_bounds('day', now=now)

    assert start_at == datetime(2026, 3, 15, 0, 0, tzinfo=UTC)
    assert end_at == now


def test_campaign_period_bounds_week():
    now = datetime(2026, 3, 15, 13, 45, tzinfo=UTC)  # Sunday
    start_at, end_at = get_campaign_period_bounds('week', now=now)

    assert start_at == datetime(2026, 3, 9, 0, 0, tzinfo=UTC)  # Monday
    assert end_at == now


def test_campaign_period_bounds_month_previous_month_year():
    now = datetime(2026, 3, 15, 13, 45, tzinfo=UTC)

    month_start_at, month_end_at = get_campaign_period_bounds('month', now=now)
    assert month_start_at == datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    assert month_end_at == now

    previous_month_start_at, previous_month_end_at = get_campaign_period_bounds('previous_month', now=now)
    assert previous_month_start_at == datetime(2026, 2, 1, 0, 0, tzinfo=UTC)
    assert previous_month_end_at == datetime(2026, 3, 1, 0, 0, tzinfo=UTC)

    year_start_at, year_end_at = get_campaign_period_bounds('year', now=now)
    assert year_start_at == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert year_end_at == now


def test_campaign_period_bounds_invalid_period():
    with pytest.raises(ValueError):
        get_campaign_period_bounds('quarter')
