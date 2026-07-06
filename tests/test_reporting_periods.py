"""Auto-report scheduling: weekly on Mondays, monthly on the 1st (calendar month)."""

from __future__ import annotations

from datetime import date, datetime, time as datetime_time, timedelta
from zoneinfo import ZoneInfo

from app.services.reporting_service import (
    ReportingService,
    ReportPeriod,
)


MSK = ZoneInfo('Europe/Moscow')


def _svc() -> ReportingService:
    return ReportingService()


# ---------- extra periods for a run date ----------


def test_plain_weekday_sends_nothing_extra():
    # 2026-07-08 — среда, не 1-е число
    assert _svc()._extra_periods_for(date(2026, 7, 8)) == []


def test_monday_sends_weekly():
    # 2026-07-06 — понедельник
    extra = _svc()._extra_periods_for(date(2026, 7, 6))
    assert (ReportPeriod.WEEKLY, date(2026, 7, 6)) in extra
    assert all(p != ReportPeriod.MONTHLY for p, _ in extra)


def test_first_of_month_sends_monthly():
    # 2026-08-01 — суббота, 1-е число
    extra = _svc()._extra_periods_for(date(2026, 8, 1))
    assert (ReportPeriod.MONTHLY, date(2026, 8, 1)) in extra
    assert all(p != ReportPeriod.WEEKLY for p, _ in extra)


def test_monday_first_of_month_sends_both():
    # 2027-02-01 — понедельник и 1-е число
    extra = _svc()._extra_periods_for(date(2027, 2, 1))
    periods = [p for p, _ in extra]
    assert ReportPeriod.WEEKLY in periods
    assert ReportPeriod.MONTHLY in periods


# ---------- period ranges ----------


def test_weekly_range_from_monday_covers_previous_week():
    rng = _svc()._get_period_range(ReportPeriod.WEEKLY, date(2026, 7, 6))  # понедельник
    assert rng.start_msk == datetime(2026, 6, 29, 0, 0, tzinfo=MSK)  # прошлый понедельник
    assert rng.end_msk == datetime(2026, 7, 6, 0, 0, tzinfo=MSK)


def test_monthly_range_on_first_covers_previous_calendar_month():
    rng = _svc()._get_period_range(ReportPeriod.MONTHLY, date(2026, 7, 1))
    assert rng.start_msk == datetime(2026, 6, 1, 0, 0, tzinfo=MSK)
    assert rng.end_msk == datetime(2026, 7, 1, 0, 0, tzinfo=MSK)


def test_monthly_range_on_first_after_31_day_month():
    rng = _svc()._get_period_range(ReportPeriod.MONTHLY, date(2026, 8, 1))
    assert rng.start_msk == datetime(2026, 7, 1, 0, 0, tzinfo=MSK)
    assert rng.end_msk == datetime(2026, 8, 1, 0, 0, tzinfo=MSK)


def test_monthly_range_mid_month_keeps_rolling_30_days():
    # ручной запуск из админки не с 1-го числа — прежняя семантика (30 дней)
    rng = _svc()._get_period_range(ReportPeriod.MONTHLY, date(2026, 7, 15))
    assert rng.end_msk == datetime(2026, 7, 15, 0, 0, tzinfo=MSK)
    assert rng.end_msk - rng.start_msk == timedelta(days=30)


def test_daily_calculate_next_run_unchanged():
    svc = _svc()
    next_run_utc, report_date = svc._calculate_next_run(datetime_time(hour=9, minute=0))
    candidate_msk = next_run_utc.astimezone(MSK)
    assert candidate_msk.time() == datetime_time(hour=9, minute=0)
    assert report_date == candidate_msk.date() - timedelta(days=1)
