"""Regression test for transaction-description pluralization in autopay paths.

Telegram bug report #595885 (sub-issue): the autopay description was hard-coded
to "Автопродление истёкшей подписки на N дней" — incorrect for N=1
("на 1 дней" should be "на 1 день") and N=2,3,4 ("на 2 дней" should be
"на 2 дня"). The fix routes all such strings through
`app.utils.formatters.format_days_declension`.

These tests pin the public helper's behaviour and exercise it the same way
the description-building code does.
"""

from __future__ import annotations

import pytest

from app.utils.formatters import format_days_declension


@pytest.mark.parametrize(
    ('days', 'expected'),
    [
        (1, '1 день'),
        (2, '2 дня'),
        (3, '3 дня'),
        (4, '4 дня'),
        (5, '5 дней'),
        (11, '11 дней'),  # special case — 11–14 always "дней"
        (12, '12 дней'),
        (14, '14 дней'),
        (21, '21 день'),
        (22, '22 дня'),
        (25, '25 дней'),
        (30, '30 дней'),
        (31, '31 день'),
        (100, '100 дней'),
        (101, '101 день'),
        (111, '111 дней'),
        (365, '365 дней'),
    ],
)
def test_format_days_declension_ru(days: int, expected: str) -> None:
    """The Russian declension matters at the user-visible boundary —
    transaction history shows these strings."""
    assert format_days_declension(days, 'ru') == expected


def test_autopay_description_uses_correct_plural_for_one_day() -> None:
    """End-to-end shape: the autopay path renders
    'Автопродление истёкшей подписки на N <day_word>' — assert that 1 day
    produces 'на 1 день' (not the buggy 'на 1 дней')."""
    period_days = 1
    description = f'Автопродление истёкшей подписки на {format_days_declension(period_days)}'
    assert description == 'Автопродление истёкшей подписки на 1 день'


def test_autopay_description_uses_correct_plural_for_few_days() -> None:
    period_days = 3
    description = f'Автопродление истёкшей подписки на {format_days_declension(period_days)}'
    assert description == 'Автопродление истёкшей подписки на 3 дня'


def test_autopay_description_uses_correct_plural_for_many_days() -> None:
    period_days = 30
    description = f'Автопродление истёкшей подписки на {format_days_declension(period_days)}'
    assert description == 'Автопродление истёкшей подписки на 30 дней'
