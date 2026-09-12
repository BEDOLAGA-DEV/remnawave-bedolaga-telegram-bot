"""Границы календарного дня в settings.TIMEZONE, выраженные в UTC (#3136).

«Доход за сегодня» считался от 00:00 UTC, а не от полуночи настроенной зоны:
при Europe/Moscow платежи с 00:00 до 02:59 МСК уезжали во вчера. Здесь —
единственное определение «сегодня», которым обязаны пользоваться все отчёты.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.utils.timezone import local_date, local_day_bounds, local_day_start
from tests.fixtures.local_day import reset_local_timezone_cache, use_timezone  # noqa: F401


MOSCOW = ZoneInfo('Europe/Moscow')
BERLIN = ZoneInfo('Europe/Berlin')


def test_bounds_of_moscow_day_are_utc_instants():
    moment = datetime(2026, 9, 11, 21, 30, tzinfo=UTC)  # 00:30 МСК 12 сентября

    start, end = local_day_bounds(moment, tz=MOSCOW)

    assert start == datetime(2026, 9, 11, 21, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 12, 21, 0, tzinfo=UTC)
    assert start.tzinfo is UTC
    assert end.tzinfo is UTC


def test_moment_before_moscow_midnight_belongs_to_previous_day():
    moment = datetime(2026, 9, 11, 20, 59, tzinfo=UTC)  # 23:59 МСК 11 сентября

    start, end = local_day_bounds(moment, tz=MOSCOW)

    assert start == datetime(2026, 9, 10, 21, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 11, 21, 0, tzinfo=UTC)


def test_local_date_follows_the_zone_not_utc():
    assert local_date(datetime(2026, 9, 11, 22, 30, tzinfo=UTC), tz=MOSCOW) == date(2026, 9, 12)


def test_naive_moment_is_treated_as_utc():
    start, _ = local_day_bounds(datetime(2026, 9, 11, 21, 30), tz=MOSCOW)

    assert start == datetime(2026, 9, 11, 21, 0, tzinfo=UTC)


def test_spring_forward_day_is_23_hours_long():
    """Границы считаются через ZoneInfo, а не через фиксированное смещение."""
    moment = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)  # день перевода часов в ЕС

    start, end = local_day_bounds(moment, tz=BERLIN)

    assert start == datetime(2026, 3, 28, 23, 0, tzinfo=UTC)  # 00:00 CET
    assert end == datetime(2026, 3, 29, 22, 0, tzinfo=UTC)  # 00:00 CEST
    assert end - start == timedelta(hours=23)


def test_fall_back_day_is_25_hours_long():
    moment = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

    start, end = local_day_bounds(moment, tz=BERLIN)

    assert end - start == timedelta(hours=25)


def test_days_back_counts_calendar_days_across_dst():
    moment = datetime(2026, 3, 30, 12, 0, tzinfo=UTC)

    start = local_day_start(moment, tz=BERLIN, days_back=2)

    assert start == datetime(2026, 3, 27, 23, 0, tzinfo=UTC)  # 00:00 CET 28 марта, не «минус 48 часов»


def test_default_zone_comes_from_settings(monkeypatch, reset_local_timezone_cache):
    use_timezone(monkeypatch, 'Europe/Moscow')

    start, _ = local_day_bounds(datetime(2026, 9, 11, 21, 30, tzinfo=UTC))

    assert start == datetime(2026, 9, 11, 21, 0, tzinfo=UTC)


def test_utc_zone_keeps_utc_midnight(monkeypatch, reset_local_timezone_cache):
    use_timezone(monkeypatch, 'UTC')

    start, end = local_day_bounds(datetime(2026, 9, 12, 5, 0, tzinfo=UTC))

    assert start == datetime(2026, 9, 12, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 13, 0, 0, tzinfo=UTC)


def test_month_start_is_local_first_day_midnight_in_utc():
    from app.utils.timezone import local_month_start

    moment = datetime(2026, 8, 31, 22, 30, tzinfo=UTC)  # 01:30 МСК 1 сентября

    assert local_month_start(moment, tz=MOSCOW) == datetime(2026, 8, 31, 21, 0, tzinfo=UTC)


def test_month_start_before_local_midnight_is_previous_month():
    from app.utils.timezone import local_month_start

    moment = datetime(2026, 8, 31, 20, 30, tzinfo=UTC)  # 23:30 МСК 31 августа

    assert local_month_start(moment, tz=MOSCOW) == datetime(2026, 7, 31, 21, 0, tzinfo=UTC)
