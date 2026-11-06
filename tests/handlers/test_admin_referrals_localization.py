"""Referral admin rendering preserves data and callback contracts in all locales."""

import copy
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.admin import referrals
from app.keyboards.withdrawal import get_withdrawal_request_keyboard
from app.services.referral_withdrawal_service import localize_withdrawal_detail, referral_withdrawal_service


LANGUAGES = ['ru', 'en', 'ua', 'zh', 'fa']


@pytest.mark.parametrize('language', LANGUAGES)
def test_referral_helpers_and_withdrawal_keyboard_use_viewer_language(language):
    levels = [{'level': 1, 'money_kopeks': 100, 'days': 2}, {'level': 2, 'money_kopeks': 200, 'days': 3}]
    blocks = [
        referrals._levels_breakdown_block(levels, language),
        referrals._get_period_display_name('today', language),
    ]
    keyboard = referrals._get_top_keyboard('week', 'earnings', language)
    withdrawal = get_withdrawal_request_keyboard(7, 'pending', user_db_id=42, navigation=True, language=language)
    buttons = [b for row in keyboard.inline_keyboard + withdrawal.inline_keyboard for b in row]
    baseline = get_withdrawal_request_keyboard(7, 'pending', user_db_id=42, navigation=True)
    assert [b.callback_data for row in withdrawal.inline_keyboard for b in row] == [
        b.callback_data for row in baseline.inline_keyboard for b in row
    ]
    assert get_withdrawal_request_keyboard(7, 'pending', role='moderator', language=language) is None
    rendered = '\n'.join(blocks + [b.text for b in buttons])
    assert 'ADMIN_' not in rendered
    if language in {'en', 'zh', 'fa'}:
        assert re.search('[А-Яа-яЁё]', rendered) is None, rendered


@pytest.mark.parametrize('language', LANGUAGES)
def test_withdrawal_analysis_localizes_saved_flags_without_mutating_data(language):
    analysis = {
        'risk_level': 'high',
        'risk_score': 55,
        'recommendation_text': '🟠 ТРЕБУЕТ ПРОВЕРКИ',
        'flags': ['🔴 Пополнил 100₽, но ничего не покупал!', '⚠️ Весь доход от одного реферала'],
        'details': {
            'balance_stats': {'total_earned': 10000, 'own_deposits': 20000, 'spending': 0, 'withdrawn': 300},
            'suspicious_referrals': [
                {
                    'name': 'Example <name>',
                    'deposits_count': 5,
                    'deposits_total': 10000,
                    'flags': ['5 пополнений/мес', 'сумма 100₽'],
                }
            ],
            'earnings_by_reason': {'referral_first_topup': {'count': 3, 'total': 10000}},
        },
    }
    original = copy.deepcopy(analysis)
    text = referral_withdrawal_service.format_analysis_for_admin(analysis, language)
    assert analysis == original
    assert 'Example &lt;name&gt;' in text
    assert '55/100' in text and '100₽' in text
    assert localize_withdrawal_detail('custom operator detail', language) == 'custom operator detail'
    assert localize_withdrawal_detail('сумма custom₽', language) == 'сумма custom₽'
    if language in {'en', 'zh', 'fa'}:
        assert re.search('[А-Яа-яЁё]', text) is None, text
        assert (
            re.search('[А-Яа-яЁё]', localize_withdrawal_detail('Недостаточно средств на балансе. Баланс: 9₽', language))
            is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_approval_error_keeps_service_arguments_and_localizes_feedback(monkeypatch, language):
    callback = SimpleNamespace(data='admin_withdrawal_approve_7', answer=AsyncMock())
    result = SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(id=7, user_id=42))
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    approve = AsyncMock(return_value=(False, 'Заявка уже обработана'))
    monkeypatch.setattr(referral_withdrawal_service, 'approve_request', approve)
    handler = referrals.approve_withdrawal_request
    while hasattr(handler, '__wrapped__'):
        handler = handler.__wrapped__
    await handler(callback, SimpleNamespace(id=123, language=language), db)
    approve.assert_awaited_once_with(db, 7, 123)
    callback.answer.assert_awaited_once_with(
        '❌ ' + localize_withdrawal_detail('Заявка уже обработана', language), show_alert=True
    )


@pytest.mark.parametrize('language', ['en', 'zh', 'fa'])
def test_diagnostic_service_errors_have_explicit_translations(language):
    for value in referrals._DIAGNOSTIC_ERROR_KEYS:
        assert re.search('[А-Яа-яЁё]', referrals._localize_diagnostic_error(value, language)) is None
    assert referrals._localize_diagnostic_error('custom exception', language) == 'custom exception'
