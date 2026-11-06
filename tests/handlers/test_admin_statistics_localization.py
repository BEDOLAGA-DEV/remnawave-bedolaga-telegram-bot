"""Statistics use the admin's language and preserve data, periods and amounts."""

import inspect
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.admin import statistics as ui
from app.localization.texts import get_texts


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_user_statistics_preserves_numbers_and_callbacks(monkeypatch, language):
    stats = dict(
        total_users=100, active_users=25, blocked_users=5, deleted_users=2, new_today=1, new_week=7, new_month=20
    )
    service = SimpleNamespace(get_user_statistics=AsyncMock(return_value=stats))
    monkeypatch.setattr(ui, 'UserService', lambda: service)
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    await inspect.unwrap(ui.show_users_statistics)(callback, SimpleNamespace(language=language), None)
    text = callback.message.edit_text.await_args.args[0]
    assert '100' in text and '25' in text and '20' in text
    assert 'ADMIN_STATISTICS_' not in text
    if language in {'en', 'zh', 'fa'}:
        assert not re.search('[А-Яа-яЁё]', text)
    keyboard = callback.message.edit_text.await_args.kwargs['reply_markup']
    assert [button.callback_data for row in keyboard.inline_keyboard for button in row] == [
        'admin_stats_users',
        'admin_statistics',
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_revenue_period_label_does_not_change_query(monkeypatch, language):
    get_revenue = AsyncMock(return_value=[])
    monkeypatch.setattr(ui, 'get_revenue_by_period', get_revenue)
    callback = SimpleNamespace(data='period_week', message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    db = object()
    await inspect.unwrap(ui.show_revenue_by_period)(callback, SimpleNamespace(language=language), db)
    get_revenue.assert_awaited_once_with(db, 7)
    assert get_texts(language).t('ADMIN_STATISTICS_PERIOD_WEEK') in callback.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_referral_statistics_preserves_days_and_escapes_operator_names(monkeypatch, language):
    stats = dict(
        users_with_referrals=3,
        active_referrers=2,
        total_paid_kopeks=0,
        total_paid_days=14,
        today_earnings_kopeks=0,
        today_earnings_days=4,
        week_earnings_kopeks=0,
        week_earnings_days=9,
        month_earnings_kopeks=0,
        month_earnings_days=14,
        top_referrers=[
            dict(display_name='<Example & name>', total_earned_kopeks=0, total_earned_days=14, referrals_count=3)
        ],
    )
    monkeypatch.setattr(ui, 'get_referral_statistics', AsyncMock(return_value=stats))
    original = ui.format_reward_total
    reward_calls = []

    def reward(money, days, language=None):
        reward_calls.append((money, days, language))
        return original(money, days, language=language)

    monkeypatch.setattr(ui, 'format_reward_total', reward)
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    await inspect.unwrap(ui.show_referral_statistics)(callback, SimpleNamespace(language=language), None)
    text = callback.message.edit_text.await_args.args[0]
    assert '&lt;Example &amp; name&gt;' in text and '<Example' not in text
    assert reward_calls == [(0, 14, language), (0, 4, language), (0, 9, language), (0, 14, language), (0, 14, language)]
