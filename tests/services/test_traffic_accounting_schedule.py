"""Keep the panel's calendar anchors when the bot owns entitlement refresh."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.traffic_accounting_schedule import next_base_refresh


@pytest.mark.parametrize(
    ('strategy', 'expected'),
    [
        ('DAY', '2026-01-16T00:05:00+00:00'),
        ('WEEK', '2026-01-19T00:15:00+00:00'),
        ('MONTH', '2026-02-01T00:20:00+00:00'),
        ('MONTH_ROLLING', '2026-01-31T00:10:00+00:00'),
    ],
)
def test_calendar_strategies_preserve_panel_schedule(strategy, expected):
    result = next_base_refresh(
        strategy,
        now=datetime(2026, 1, 15, 12, tzinfo=UTC),
        anchor=datetime(2025, 12, 31, tzinfo=UTC),
        timezone=ZoneInfo('UTC'),
    )
    assert result == datetime.fromisoformat(expected)


@pytest.mark.parametrize(('year', 'day'), [(2026, 28), (2028, 29)])
def test_rolling_month_clamps_end_of_month(year, day):
    result = next_base_refresh(
        'MONTH_ROLLING',
        now=datetime(year, 2, 2, tzinfo=UTC),
        anchor=datetime(year, 1, 31, tzinfo=UTC),
        timezone=ZoneInfo('UTC'),
    )
    assert result == datetime(year, 2, day, 0, 10, tzinfo=UTC)


def test_rolling_anchor_recovers_31_after_february():
    result = next_base_refresh(
        'MONTH_ROLLING',
        now=datetime(2026, 3, 1, tzinfo=UTC),
        anchor=datetime(2026, 1, 31, tzinfo=UTC),
        timezone=ZoneInfo('UTC'),
    )
    assert result == datetime(2026, 3, 31, 0, 10, tzinfo=UTC)


def test_new_rolling_subscription_does_not_reset_on_creation_day():
    result = next_base_refresh(
        'MONTH_ROLLING',
        now=datetime(2026, 1, 15, 0, 1, tzinfo=UTC),
        anchor=datetime(2026, 1, 15, tzinfo=UTC),
        timezone=ZoneInfo('UTC'),
    )
    assert result == datetime(2026, 2, 15, 0, 10, tzinfo=UTC)


def test_boundary_is_strictly_after_current_time():
    now = datetime(2026, 1, 1, 0, 20, tzinfo=UTC)
    assert next_base_refresh('MONTH', now=now, anchor=now, timezone=ZoneInfo('UTC')) == datetime(
        2026, 2, 1, 0, 20, tzinfo=UTC
    )


def test_explicit_panel_timezone_is_respected():
    now = datetime(2026, 1, 1, 22, tzinfo=UTC)
    result = next_base_refresh('DAY', now=now, anchor=now, timezone=ZoneInfo('Europe/Moscow'))
    assert result == datetime(2026, 1, 2, 21, 5, tzinfo=UTC)


def test_no_reset_has_no_schedule():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert next_base_refresh('NO_RESET', now=now, anchor=now, timezone=ZoneInfo('UTC')) is None
