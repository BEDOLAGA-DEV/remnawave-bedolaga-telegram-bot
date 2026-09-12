"""Статистика продаж в кабинете: пресеты «N дней» и даты без зоны — календарные дни settings.TIMEZONE (#3136)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.cabinet.routes.admin_sales_stats import _parse_period
from tests.fixtures.local_day import reset_local_timezone_cache, use_timezone  # noqa: F401


MOSCOW = ZoneInfo('Europe/Moscow')


def test_preset_days_start_at_local_midnight(monkeypatch, reset_local_timezone_cache):
    use_timezone(monkeypatch, 'Europe/Moscow')
    today_local = datetime.now(MOSCOW).date()

    start, end = _parse_period(7, None, None)

    expected_day = today_local - timedelta(days=7)
    assert start == datetime(expected_day.year, expected_day.month, expected_day.day, tzinfo=MOSCOW).astimezone(UTC)
    assert end.tzinfo is UTC
    assert end <= datetime.now(UTC)


def test_custom_dates_without_zone_are_local_calendar_days(monkeypatch, reset_local_timezone_cache):
    use_timezone(monkeypatch, 'Europe/Moscow')

    start, end = _parse_period(None, '2026-09-01', '2026-09-02')

    assert start == datetime(2026, 8, 31, 21, 0, tzinfo=UTC)  # 00:00 МСК 1 сентября
    assert end == datetime(2026, 9, 2, 21, 0, tzinfo=UTC) - timedelta(microseconds=1)  # конец 2 сентября по МСК


def test_custom_dates_with_zone_are_kept_as_instants(monkeypatch, reset_local_timezone_cache):
    use_timezone(monkeypatch, 'Europe/Moscow')

    start, end = _parse_period(None, '2026-09-01T10:00:00+00:00', '2026-09-02T10:00:00+00:00')

    assert start == datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
