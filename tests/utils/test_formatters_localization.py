from datetime import UTC, datetime, timedelta

import pytest

from app.utils.formatters import (
    format_boolean,
    format_days_declension,
    format_subscription_status,
    format_time_ago,
    format_traffic_usage,
)


@pytest.mark.parametrize(
    'language,yes,days,yesterday',
    [
        ('ru', '✅ Да', '3 дня', 'вчера'),
        ('en', '✅ Yes', '3 days', 'yesterday'),
        ('ua', '✅ Так', '3 дні', 'вчора'),
        ('zh', '✅ 是', '3 天', '昨天'),
        ('fa', '✅ بله', '3 روز', 'دیروز'),
    ],
)
def test_common_formatters_use_selected_language(language, yes, days, yesterday):
    assert format_boolean(True, language) == yes
    assert format_days_declension(3, language) == days
    assert format_time_ago(datetime.now(UTC) - timedelta(days=1, minutes=1), language) == yesterday


@pytest.mark.parametrize(
    'language,inactive,gb', [('ua', '❌ Неактивна', 'ГБ'), ('zh', '❌ 未激活', 'GB'), ('fa', '❌ غیرفعال', 'گیگابایت')]
)
def test_status_and_traffic_are_localized(language, inactive, gb):
    assert format_subscription_status(False, False, datetime.now(UTC), language) == inactive
    assert format_traffic_usage(1.5, 10, language) == f'1.5 {gb} / 10 {gb} (15.0%)'
    assert format_traffic_usage(1.5, 0, language) == f'1.5 {gb} / ∞'


@pytest.mark.parametrize(
    'language,expected', [('ru', '2 ч.'), ('en', '2 hrs'), ('ua', '2 год.'), ('zh', '2 小时'), ('fa', '2 ساعت')]
)
def test_duration_uses_selected_language(language, expected):
    from app.utils.formatters import format_duration

    assert format_duration(7200, language) == expected
