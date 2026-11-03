"""Payment notice rendering uses the recipient language and preserves financial values."""

import ast
import importlib
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import InlineKeyboardMarkup

from app.config import Settings
from app.localization.texts import get_texts
from app.services.payment.cloudpayments import CloudPaymentsPaymentMixin
from app.services.payment.common import PaymentCommonMixin


LANGUAGES = ['ru', 'en', 'ua', 'zh', 'fa']
PAYMENT_DIR = Path(__file__).resolve().parents[2] / 'app/services/payment'
PROVIDERS = [
    'antilopay',
    'aurapay',
    'cashera',
    'cloudpayments',
    'common',
    'cryptobot',
    'donut',
    'etoplatezhi',
    'freekassa',
    'heleket',
    'jupiter',
    'kassa_ai',
    'lava',
    'mulenpay',
    'overpay',
    'pal24',
    'paritypay',
    'platega',
    'riopay',
    'rollypay',
    'severpay',
    'stars',
    'tabpay',
    'wata',
    'yookassa',
]


def _assert_language(text, language):
    assert 'PAYMENT_NOTICE_' not in text
    if language in {'en', 'zh', 'fa'}:
        assert re.search('[А-Яа-яЁё]', text.replace('Оператор', '')) is None, text


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('language', LANGUAGES)
def test_provider_actual_notice_expressions_preserve_payload_and_precision(provider, language):
    """Execute the expressions used by senders, with deterministic payment snapshots."""
    module = importlib.import_module(f'app.services.payment.{provider}')
    tree = ast.parse((PAYMENT_DIR / f'{provider}.py').read_text())
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    env = dict(vars(module))
    env.update(
        get_texts=get_texts,
        user=SimpleNamespace(language=language),
        user_snapshot=SimpleNamespace(language=language),
        language=language,
        payment=SimpleNamespace(amount_kopeks=12345),
        transaction=SimpleNamespace(id=987),
        updated_payment=SimpleNamespace(payer_currency='USDT', payer_amount='1.23456', amount='1.23456', asset='USDT'),
        amount_kopeks=12345,
        display_name='Оператор',
        display_name_html='Оператор',
        method_title='Оператор',
        payment_method='Оператор',
        exchange_rate_value=3,
        conversion_rate=91.23456,
        invoice_id='masked-12345',
        subscription_period=30,
        period_display=30,
        subscription=SimpleNamespace(device_limit=7),
        _t=SimpleNamespace(name='Оператор'),
        tariff_line='\n📦 Оператор',
        stars_amount=37,
        traffic_label='15 GB',
        traffic_limit=15,
        message='Opaque provider detail',
        settings=SimpleNamespace(
            format_price=lambda amount: f'{amount / 100:.2f} RUB',
            TRIAL_DURATION_DAYS=3,
            get_cashera_display_name_html=lambda: 'Оператор',
        ),
    )
    count = 0
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 't'
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and str(node.args[0].value).startswith('PAYMENT_NOTICE_')
        ):
            continue
        count += 1
        expression = node
        parent = parents.get(node)
        if isinstance(parent, ast.Attribute) and parent.attr == 'format':
            expression = parents[parent]
        result = eval(compile(ast.Expression(expression), str(module.__file__), 'eval'), env)
        _assert_language(result, language)
        key = node.args[0].value
        if key == 'PAYMENT_NOTICE_001':
            assert '0.3333 USDT' in result
        if key == 'PAYMENT_NOTICE_019':
            assert '91.23₽' in result
            assert '1.23456 USDT' in result
            assert 'masked-1...' in result
        if key in {'PAYMENT_NOTICE_000', 'PAYMENT_NOTICE_007'}:
            assert '123.45 RUB' in result
            assert 'Оператор' in result
            assert '987' in result
    assert count > 0


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_common_notice_uses_account_language_and_preserves_custom_method(monkeypatch, language):
    service = PaymentCommonMixin()
    service.bot = SimpleNamespace(send_message=AsyncMock())
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    service.build_topup_success_keyboard = AsyncMock(return_value=keyboard)
    monkeypatch.setattr(Settings, 'is_notifications_enabled', lambda self: True)
    websocket = AsyncMock()
    monkeypatch.setattr('app.cabinet.routes.websocket.notify_user_balance_topup', websocket)
    user = SimpleNamespace(id=7, telegram_id=123, language=language, balance_kopeks=67890, subscriptions=[])
    await service._send_payment_success_notification(123, 12345, user=user, payment_method_title='Оператор')
    call = service.bot.send_message.await_args
    assert call.kwargs['chat_id'] == 123
    assert 'Оператор' in call.kwargs['text']
    assert '123' in call.kwargs['text']
    _assert_language(call.kwargs['text'], language)
    assert service.build_topup_success_keyboard.await_args.args[0].language == language
    websocket.assert_awaited_once_with(user_id=7, amount_kopeks=12345, new_balance_kopeks=67890, description='Оператор')


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_cloudpayments_failure_localizes_wrapper_but_preserves_provider_detail(monkeypatch, language):
    bot = SimpleNamespace(send_message=AsyncMock())

    class BotContext:
        async def __aenter__(self):
            return bot

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr('app.bot_factory.create_bot', lambda: BotContext())
    monkeypatch.setattr(Settings, 'is_notifications_enabled', lambda self: True)
    service = CloudPaymentsPaymentMixin()
    detail = 'Opaque provider detail: masked-1'
    await service._send_cloudpayments_fail_notification(123, detail, language=language)
    assert bot.send_message.await_args.kwargs['text'] == get_texts(language).t('PAYMENT_NOTICE_020').format(v0=detail)
    assert bot.send_message.await_args.kwargs['chat_id'] == 123


def test_cloudpayments_failure_call_passes_resolved_recipient_language():
    tree = ast.parse((PAYMENT_DIR / 'cloudpayments.py').read_text())
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == '_send_cloudpayments_fail_notification'
    ]
    assert len(calls) == 1
    args = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    assert args == {
        'telegram_id': 'user.telegram_id',
        'message': 'card_holder_message',
        'language': "getattr(user, 'language', None) or settings.DEFAULT_LANGUAGE",
    }


@pytest.mark.parametrize('provider', ['mulenpay', 'cryptobot', 'yookassa'])
def test_lightweight_user_without_language_uses_configured_default(provider, monkeypatch):
    module = importlib.import_module(f'app.services.payment.{provider}')
    monkeypatch.setattr(module.settings, 'DEFAULT_LANGUAGE', 'fa')
    tree = ast.parse((PAYMENT_DIR / f'{provider}.py').read_text())
    lookups = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == 'get_texts'
        and n.args
        and 'getattr(user,' in ast.unparse(n.args[0])
    ]
    assert lookups
    env = dict(vars(module), user=SimpleNamespace())
    for lookup in lookups:
        texts = eval(compile(ast.Expression(lookup), str(module.__file__), 'eval'), env)
        assert texts.language == 'fa'
