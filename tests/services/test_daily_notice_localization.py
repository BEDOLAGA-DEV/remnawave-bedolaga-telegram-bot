"""Daily subscription notices preserve amounts, traffic context, and recipient language."""

import ast
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.localization.texts import get_texts
from app.services import daily_subscription_service as daily
from app.services.notification_delivery_service import NotificationType


LANGUAGES = ['ru', 'en', 'ua', 'zh', 'fa']


def _assert_language(text, language):
    if language in {'en', 'zh', 'fa'}:
        assert re.search('[А-Яа-яЁё]', text.replace('Оператор', '')) is None
    assert 'DAILY_NOTICE_' not in text


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_daily_notices_keep_financial_and_traffic_delivery_payload(monkeypatch, language):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    debit, send = AsyncMock(), AsyncMock()
    monkeypatch.setattr(daily.notification_delivery_service, 'notify_daily_debit', debit)
    monkeypatch.setattr(daily.notification_delivery_service, 'send_notification', send)
    service = daily.DailySubscriptionService()
    service._bot = object()
    user = SimpleNamespace(language=language, balance_kopeks=12345)
    subscription = SimpleNamespace(tariff=SimpleNamespace(name='Оператор'), traffic_limit_gb=150)
    await service._notify_daily_charge(user, subscription, 123)
    payload = debit.await_args.kwargs
    assert payload['amount_kopeks'] == 123
    assert payload['new_balance_kopeks'] == 12345
    assert payload['user'] is user
    assert payload['bot'] is service._bot
    assert '1.23 ₽' in payload['telegram_message']
    assert '123.45 ₽' in payload['telegram_message']
    assert 'Оператор' in payload['telegram_message']
    _assert_language(payload['telegram_message'], language)
    await service._notify_insufficient_balance(user, subscription, 67890)
    payload = send.await_args.kwargs
    assert payload['notification_type'] == NotificationType.DAILY_INSUFFICIENT_FUNDS
    assert payload['context'] == {'required_amount': '678.90 ₽', 'current_balance': '123.45 ₽'}
    _assert_language(payload['telegram_message'], language)
    buttons = [b for row in payload['telegram_markup'].inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == ['menu_balance', 'menu_subscription']
    _assert_language(' '.join(b.text for b in buttons), language)
    await service._notify_traffic_reset(user, subscription, 50)
    payload = send.await_args.kwargs
    assert payload['notification_type'] == NotificationType.TRAFFIC_RESET
    assert payload['context'] == {'reset_gb': 50, 'current_limit_gb': 150}
    assert '50' in payload['telegram_message']
    assert '150' in payload['telegram_message']
    _assert_language(payload['telegram_message'], language)


@pytest.mark.parametrize('language', LANGUAGES)
def test_daily_activation_actual_template_keeps_rounded_price_and_escaped_tariff(language):
    import html

    from app.services import subscription_auto_purchase_service as auto

    tree = ast.parse(Path(auto.__file__).read_text())
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == 't'
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == 'DAILY_NOTICE_006'
    ]
    assert len(calls) == 1
    expression = parents[parents[calls[0]]]
    env = dict(
        vars(auto),
        user=SimpleNamespace(language=language),
        tariff=SimpleNamespace(name='<Оператор>'),
        final_price=12345,
        html=html,
    )
    result = eval(compile(ast.Expression(expression), str(auto.__file__), 'eval'), env)
    assert '&lt;Оператор&gt;' in result
    assert '123 ₽' in result
    assert '123.45' not in result
    _assert_language(result, language)


@pytest.mark.asyncio
async def test_daily_missing_account_language_uses_configured_default(monkeypatch):
    monkeypatch.setattr(daily.settings, 'DEFAULT_LANGUAGE', 'fa')
    debit = AsyncMock()
    monkeypatch.setattr(daily.notification_delivery_service, 'notify_daily_debit', debit)
    service = daily.DailySubscriptionService()
    user = SimpleNamespace(balance_kopeks=100)
    await service._notify_daily_charge(user, SimpleNamespace(tariff=None), 100)
    assert debit.await_args.kwargs['telegram_message'] == get_texts('fa').t('DAILY_NOTICE_000').format(
        v0=1, v1=1, v2=''
    )
