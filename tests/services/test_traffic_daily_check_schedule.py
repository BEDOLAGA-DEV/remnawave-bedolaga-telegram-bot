"""TRAFFIC_DAILY_CHECK_TIME — локальное время оператора (settings.TIMEZONE), а не UTC.

Тот же класс, что BACKUP_TIME (#3030) и REMNAWAVE_AUTO_SYNC_TIMES: часы из
настроек подставлялись в UTC-«сейчас», и для Europe/Moscow проверка уезжала
на три часа.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

from app.services.traffic_monitoring_service import TrafficMonitoringSchedulerV2
from tests.fixtures.local_day import reset_local_timezone_cache, use_timezone  # noqa: F401


def test_daily_check_runs_at_local_midnight(monkeypatch, reset_local_timezone_cache):
    use_timezone(monkeypatch, 'Europe/Moscow')
    reference = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)  # 15:00 МСК

    next_run = TrafficMonitoringSchedulerV2._next_daily_check(time(0, 0), reference)

    assert next_run == datetime(2026, 9, 12, 21, 0, tzinfo=UTC)  # 00:00 МСК 13 сентября


def test_daily_check_today_if_local_time_is_still_ahead(monkeypatch, reset_local_timezone_cache):
    use_timezone(monkeypatch, 'Europe/Moscow')
    reference = datetime(2026, 9, 12, 1, 0, tzinfo=UTC)  # 04:00 МСК

    next_run = TrafficMonitoringSchedulerV2._next_daily_check(time(6, 30), reference)

    assert next_run == datetime(2026, 9, 12, 3, 30, tzinfo=UTC)  # 06:30 МСК сегодня
