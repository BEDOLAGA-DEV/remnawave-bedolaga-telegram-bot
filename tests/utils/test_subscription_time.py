"""Остаток подписки: календарные дни, точный остаток и порог автопродления.

Жалоба 4.12.0: сейчас 16.09 14:21, подписка до 18.09 14:17 — бот писал
«истекает завтра, осталось 1 дн.». ``timedelta.days`` отбрасывал неполные
сутки (1 д 23 ч 56 мин → 1), а единицу тексты читали как «завтра».
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.utils.subscription_time import (
    days_left_rounded_up,
    ends_within_days,
    format_expiry_warning,
    format_time_left,
    local_days_until,
)
from tests.fixtures.local_day import reset_local_timezone_cache, use_timezone  # noqa: F401


class _Texts:
    def t(self, key: str, default: str) -> str:
        return default


# 16.09 14:21 МСК и 18.09 14:17 МСК — ровно случай из жалобы.
NOW = datetime(2026, 9, 16, 11, 21, tzinfo=UTC)
END = datetime(2026, 9, 18, 11, 17, tzinfo=UTC)


@pytest.fixture
def moscow(monkeypatch, reset_local_timezone_cache):
    return use_timezone(monkeypatch, 'Europe/Moscow')


def test_complaint_case_is_day_after_tomorrow(moscow):
    assert local_days_until(END, NOW) == 2


def test_later_today_is_zero(moscow):
    assert local_days_until(NOW + timedelta(hours=5), NOW) == 0


def test_short_time_across_local_midnight_is_tomorrow(moscow):
    # 16.09 23:00 МСК → 17.09 01:00 МСК: два часа, но это уже завтра.
    now = datetime(2026, 9, 16, 20, 0, tzinfo=UTC)
    assert local_days_until(now + timedelta(hours=2), now) == 1


def test_calendar_day_follows_operator_zone_not_utc(moscow):
    # 16.09 22:00 UTC — в Москве уже 17.09; конец 17.09 23:00 МСК = «сегодня».
    now = datetime(2026, 9, 16, 22, 0, tzinfo=UTC)
    end = datetime(2026, 9, 17, 20, 0, tzinfo=UTC)
    assert local_days_until(end, now) == 0


def test_expired_is_zero(moscow):
    assert local_days_until(NOW - timedelta(minutes=1), NOW) == 0


def test_naive_values_are_utc(moscow):
    assert local_days_until(END.replace(tzinfo=None), NOW.replace(tzinfo=None)) == 2


def test_time_left_keeps_partial_day():
    assert format_time_left(_Texts(), END, NOW) == '1 дн. 23 ч.'


@pytest.mark.parametrize(
    ('delta', 'expected'),
    [
        (timedelta(days=10, hours=5), '10 дн. 5 ч.'),
        (timedelta(days=3), '3 дн.'),
        (timedelta(hours=5, minutes=59), '5 ч.'),
        (timedelta(minutes=42), '42 мин.'),
        (timedelta(seconds=-1), 'истёк'),
    ],
)
def test_time_left_units(delta, expected):
    assert format_time_left(_Texts(), NOW + delta, NOW) == expected


@pytest.mark.parametrize(
    ('delta', 'expected'),
    [
        (timedelta(days=3, hours=23, minutes=59), False),  # почти 4 суток — рано
        (timedelta(days=3, seconds=1), False),
        (timedelta(days=3), True),
        (timedelta(days=2, hours=23), True),
        (timedelta(seconds=1), True),
        (timedelta(0), True),
        (timedelta(hours=-1), True),  # только что истекла — автоплатёж её подбирает
    ],
)
def test_ends_within_days(delta, expected):
    assert ends_within_days(NOW + delta, 3, NOW) is expected


@pytest.mark.parametrize(
    ('end', 'expected'),
    [
        (END, ''),  # случай из жалобы: послезавтра — без предупреждения «завтра»
        (datetime(2026, 9, 16, 22, 0, tzinfo=UTC), '\n⚠️ истекает завтра!'),  # 17.09 01:00 МСК
        (datetime(2026, 9, 16, 20, 0, tzinfo=UTC), '\n⚠️ истекает сегодня!'),
        (NOW + timedelta(minutes=30), '\n🔴 истекает через несколько минут!'),
        (NOW - timedelta(minutes=1), ''),
    ],
)
def test_expiry_warning(moscow, end, expected):
    assert format_expiry_warning(_Texts(), end, NOW) == expected


@pytest.mark.parametrize(
    ('delta', 'expected'),
    [
        (timedelta(hours=20), 1),
        (timedelta(days=1, hours=23, minutes=56), 2),
        (timedelta(days=2), 2),
        (timedelta(seconds=-5), 0),
    ],
)
def test_days_left_rounded_up(delta, expected):
    assert days_left_rounded_up(NOW + delta, NOW) == expected
