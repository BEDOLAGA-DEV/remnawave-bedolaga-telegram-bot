"""Regression test for Telegram bug #650541 (auto-backup "кидает 6 файлов подряд").

When next_run was in the past, _auto_backup_loop ran a backup and advanced next_run by a
SINGLE interval; if that was still in the past (downtime, a first run scheduled in the past,
or an interval shorter than the backup duration) it looped and fired another backup
immediately — a burst of back-to-back identical backups. _next_future_run advances straight
to the next FUTURE slot, capping it at one catch-up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.backup_service import BackupService


_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)


def test_skips_all_missed_hourly_slots_in_one_step():
    interval = timedelta(hours=1)
    stale = _NOW - timedelta(hours=6)  # 6 missed hourly slots (the reported "6 файлов")

    result = BackupService._next_future_run(stale, interval, _NOW)

    assert result > _NOW
    assert result == _NOW + interval  # next future slot, not six back-to-back runs


def test_skips_missed_15min_slots():
    interval = timedelta(minutes=15)
    stale = _NOW - timedelta(minutes=50)

    result = BackupService._next_future_run(stale, interval, _NOW)

    assert _NOW < result <= _NOW + interval


def test_future_next_run_advances_by_one_interval():
    interval = timedelta(hours=1)
    future = _NOW + timedelta(minutes=30)

    result = BackupService._next_future_run(future, interval, _NOW)

    assert result == future + interval  # normal case: just the next slot
