"""Границы периода премиум-лимита по режиму сброса тарифа."""

from datetime import UTC, datetime, timedelta

import pytest

from app.external.remnawave_api import TrafficLimitStrategy
from app.utils.premium_traffic_period import (
    NO_RESET,
    normalize_mode,
    period_anchor,
    period_start_for_mode,
    resolve_period_start,
    rolling_period_start,
)


def _dt(year=2026, month=9, day=6, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


ANCHOR = _dt(2026, 1, 15, 9, 30)


class TestNormalizeMode:
    def test_accepts_plain_strings(self):
        assert normalize_mode('MONTH') == 'MONTH'
        assert normalize_mode('month_rolling') == 'MONTH_ROLLING'
        assert normalize_mode('  day  ') == 'DAY'

    def test_accepts_panel_enum(self):
        assert normalize_mode(TrafficLimitStrategy.MONTH_ROLLING) == 'MONTH_ROLLING'

    def test_unknown_mode_falls_back_to_no_reset(self):
        """Не сбросить период безопаснее, чем сбросить невпопад и подарить лимит."""
        assert normalize_mode('QUARTERLY') == NO_RESET
        assert normalize_mode(None) == NO_RESET
        assert normalize_mode(42) == NO_RESET


class TestCalendarModes:
    def test_day_starts_at_midnight_today(self):
        start = period_start_for_mode('DAY', anchor=ANCHOR, now=_dt(2026, 9, 6, 23, 59))

        assert start == _dt(2026, 9, 6, 0, 0)

    def test_week_starts_on_monday(self):
        # 6 сентября 2026 — воскресенье, неделя началась 31 августа.
        start = period_start_for_mode('WEEK', anchor=ANCHOR, now=_dt(2026, 9, 6))

        assert start == _dt(2026, 8, 31, 0, 0)
        assert start.weekday() == 0

    def test_month_starts_on_the_first(self):
        start = period_start_for_mode('MONTH', anchor=ANCHOR, now=_dt(2026, 9, 6))

        assert start == _dt(2026, 9, 1, 0, 0)

    @pytest.mark.parametrize('mode', ['DAY', 'WEEK', 'MONTH'])
    def test_period_never_starts_before_the_subscription(self, mode):
        """Иначе расход считался бы с даты, когда подписки ещё не было."""
        anchor = _dt(2026, 9, 6, 14, 0)

        start = period_start_for_mode(mode, anchor=anchor, now=_dt(2026, 9, 6, 18, 0))

        assert start == anchor


class TestRollingMonth:
    def test_before_first_window_closes_period_is_the_anchor(self):
        start = period_start_for_mode('MONTH_ROLLING', anchor=ANCHOR, now=ANCHOR + timedelta(days=29))

        assert start == ANCHOR

    def test_window_rolls_every_thirty_days(self):
        start = period_start_for_mode('MONTH_ROLLING', anchor=ANCHOR, now=ANCHOR + timedelta(days=31))

        assert start == ANCHOR + timedelta(days=30)

    def test_many_windows_later(self):
        start = period_start_for_mode('MONTH_ROLLING', anchor=ANCHOR, now=ANCHOR + timedelta(days=95))

        assert start == ANCHOR + timedelta(days=90)

    def test_exactly_on_the_boundary_opens_the_new_window(self):
        start = rolling_period_start(ANCHOR, ANCHOR + timedelta(days=30))

        assert start == ANCHOR + timedelta(days=30)

    def test_now_before_anchor_gives_the_anchor(self):
        start = rolling_period_start(ANCHOR, ANCHOR - timedelta(days=5))

        assert start == ANCHOR

    def test_rolling_keeps_the_time_of_day_of_the_anchor(self):
        """Панель отсчитывает окно от момента подключения, а не от полуночи."""
        start = period_start_for_mode('MONTH_ROLLING', anchor=ANCHOR, now=ANCHOR + timedelta(days=45))

        assert start.hour == ANCHOR.hour
        assert start.minute == ANCHOR.minute


class TestNoReset:
    def test_period_never_moves(self):
        far_future = ANCHOR + timedelta(days=900)

        assert period_start_for_mode('NO_RESET', anchor=ANCHOR, now=far_future) == ANCHOR

    def test_unknown_mode_behaves_like_no_reset(self):
        assert period_start_for_mode('WHATEVER', anchor=ANCHOR, now=_dt()) == ANCHOR


class TestPanelResetCorrection:
    def test_early_panel_reset_moves_the_period(self):
        """Смена тарифа со сбросом трафика — премиум едет следом."""
        now = _dt(2026, 9, 20)
        panel_reset = _dt(2026, 9, 15, 10, 0)

        start = resolve_period_start('MONTH', anchor=ANCHOR, now=now, panel_reset_at=panel_reset)

        assert start == panel_reset

    def test_stale_panel_reset_is_ignored(self):
        """Сброс из прошлого периода границу не двигает."""
        now = _dt(2026, 9, 20)

        start = resolve_period_start('MONTH', anchor=ANCHOR, now=now, panel_reset_at=_dt(2026, 8, 3))

        assert start == _dt(2026, 9, 1, 0, 0)

    def test_acknowledged_reset_does_not_move_the_period(self):
        """Админ сбросил только обычный трафик — премиум не задет."""
        now = _dt(2026, 9, 20)
        panel_reset = _dt(2026, 9, 15, 10, 0)

        start = resolve_period_start(
            'MONTH',
            anchor=ANCHOR,
            now=now,
            panel_reset_at=panel_reset,
            acknowledged_panel_reset_at=panel_reset,
        )

        assert start == _dt(2026, 9, 1, 0, 0)

    def test_newer_reset_after_an_acknowledged_one_still_counts(self):
        now = _dt(2026, 9, 20)

        start = resolve_period_start(
            'MONTH',
            anchor=ANCHOR,
            now=now,
            panel_reset_at=_dt(2026, 9, 18),
            acknowledged_panel_reset_at=_dt(2026, 9, 15),
        )

        assert start == _dt(2026, 9, 18)

    def test_reset_from_the_future_is_ignored(self):
        """Рассинхрон часов панели и бота не должен обнулять расход."""
        now = _dt(2026, 9, 20)

        start = resolve_period_start('MONTH', anchor=ANCHOR, now=now, panel_reset_at=_dt(2026, 9, 25))

        assert start == _dt(2026, 9, 1, 0, 0)

    def test_no_reset_mode_still_follows_an_early_panel_reset(self):
        """Панель обнулила счётчик — премиум обязан начать заново, иначе разъедутся."""
        now = _dt(2026, 9, 20)
        panel_reset = _dt(2026, 9, 15)

        start = resolve_period_start('NO_RESET', anchor=ANCHOR, now=now, panel_reset_at=panel_reset)

        assert start == panel_reset

    def test_without_panel_data_the_computed_period_wins(self):
        start = resolve_period_start('MONTH', anchor=ANCHOR, now=_dt(2026, 9, 20), panel_reset_at=None)

        assert start == _dt(2026, 9, 1, 0, 0)


class TestAnchor:
    def test_first_connection_wins(self):
        first = _dt(2026, 3, 1)
        started = _dt(2026, 2, 1)

        assert period_anchor(first, started, fallback=_dt()) == first

    def test_subscription_start_is_used_before_first_connection(self):
        started = _dt(2026, 2, 1)

        assert period_anchor(None, started, fallback=_dt()) == started

    def test_fallback_when_nothing_is_known(self):
        fallback = _dt(2026, 5, 5)

        assert period_anchor(None, None, fallback=fallback) == fallback


class TestNaiveDatetimes:
    def test_naive_input_is_treated_as_utc(self):
        """Колонки времени в проекте aware, но данные извне бывают наивными."""
        naive_anchor = datetime(2026, 1, 15, 9, 30)

        start = period_start_for_mode('NO_RESET', anchor=naive_anchor, now=_dt())

        assert start == ANCHOR
        assert start.tzinfo is not None
