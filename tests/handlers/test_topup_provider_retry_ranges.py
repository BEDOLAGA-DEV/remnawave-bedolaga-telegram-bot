"""Rejected top-ups must explain an enterable range and keep manual retry possible."""

import inspect
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import settings
from app.states import BalanceStates


PROVIDERS = [
    'antilopay',
    'aurapay',
    'cashera',
    'cispay',
    'cloudpayments',
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
    'tabpay',
    'wata',
    'yookassa',
]
ENTRIES = [(name, f'process_{name}_payment_amount') for name in PROVIDERS] + [
    ('yookassa', 'process_yookassa_sbp_payment_amount'),
    ('cloudpayments', 'process_cloudpayments_amount'),
]


class ProviderSettings:
    def __getattr__(self, name):
        if name.endswith('_MIN_AMOUNT_KOPEKS'):
            return 5055
        if name.endswith('_MAX_AMOUNT_KOPEKS'):
            return 5099
        if name.startswith('is_') and name.endswith('_enabled'):
            return lambda: True
        if name == 'YOOKASSA_SBP_ENABLED':
            return True
        return getattr(settings, name)


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'fa', 'zh'])
@pytest.mark.parametrize('module_name,entry', ENTRIES, ids=[entry for _, entry in ENTRIES])
@pytest.mark.parametrize('bound', ['low', 'high'])
async def test_rejection_keeps_selection_and_shows_exact_enterable_range(
    monkeypatch, module_name, entry, language, bound
):
    module = import_module(f'app.handlers.balance.{module_name}')
    monkeypatch.setattr(module, 'settings', ProviderSettings())
    factory = MagicMock()
    monkeypatch.setattr(module, 'PaymentService', factory)
    if module_name in {'platega', 'cashera'}:
        monkeypatch.setattr(module, '_get_active_methods', lambda: [2, 'sbp'])
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_state(BalanceStates.waiting_for_amount)
    selection = {
        'payment_method': module_name,
        'platega_method': 2,
        'cashera_method': 'sbp',
        'saved_cart': {'total_price': 10000},
    }
    await state.set_data(selection)
    message = MagicMock(spec=types.Message)
    message.answer = AsyncMock()
    message.delete = AsyncMock()
    user = SimpleNamespace(id=1, telegram_id=1, language=language, restriction_topup=False)
    fixed_limits = module_name in {'cryptobot', 'heleket'}
    amount = (4000 if bound == 'low' else 10000001) if fixed_limits else (4000 if bound == 'low' else 5100)
    message.text = f'{amount // 100}.{amount % 100:02d}'
    values = {
        'message': message,
        'db_user': user,
        'db': AsyncMock(),
        'amount_kopeks': amount,
        'state': state,
        'payment_method': module_name,
    }
    handler = inspect.unwrap(getattr(module, entry))
    await handler(**{name: values[name] for name in inspect.signature(handler).parameters})

    factory.assert_not_called()
    assert await state.get_state() == BalanceStates.waiting_for_amount.state
    assert await state.get_data() == selection
    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    minimum, maximum = ('100', '50000') if fixed_limits else ('50.55', '50.99')
    assert minimum in text
    assert maximum in text
    assert 'Например, отправьте: 51' not in text  # No whole-ruble value fits 50.55..50.99.
    keyboard = message.answer.await_args.kwargs['reply_markup']
    assert any(button.callback_data == 'balance_topup' for row in keyboard.inline_keyboard for button in row)


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'fa', 'zh'])
@pytest.mark.parametrize('provider,bound', [('cryptobot', 'low'), ('cryptobot', 'high'), ('overpay', 'low')])
async def test_currency_rejection_offers_a_ruble_input_range(monkeypatch, language, provider, bound):
    module = import_module(f'app.handlers.balance.{provider}')
    config = ProviderSettings()
    config.OVERPAY_MIN_AMOUNT_KOPEKS = 100
    config.OVERPAY_MAX_AMOUNT_KOPEKS = 10000000
    config.OVERPAY_INT_MIN_EUR = 6
    config.OVERPAY_RUB_PER_EUR = 100
    monkeypatch.setattr(module, 'settings', config)
    factory = MagicMock()
    monkeypatch.setattr(module, 'PaymentService', factory)
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_state(BalanceStates.waiting_for_amount)
    selection = {
        'payment_method': 'overpay_int' if provider == 'overpay' else 'cryptobot',
        'current_rate': 250 if bound == 'low' else 1,
        'saved_cart': {'total_price': 10000},
    }
    await state.set_data(selection)
    message = MagicMock(spec=types.Message)
    message.answer = AsyncMock()
    user = SimpleNamespace(id=1, language=language, restriction_topup=False)
    amount = 20000 if bound == 'low' else 150000
    await inspect.unwrap(getattr(module, f'process_{provider}_payment_amount'))(
        message, user, AsyncMock(), amount, state
    )

    factory.assert_not_called()
    assert await state.get_state() == BalanceStates.waiting_for_amount.state
    assert await state.get_data() == selection
    text = message.answer.await_args.args[0]
    assert ('600' if provider == 'overpay' else '250' if bound == 'low' else '100') in text
    assert ('50000' if bound == 'low' else '1000') in text
    assert any(
        button.callback_data == 'balance_topup'
        for row in message.answer.await_args.kwargs['reply_markup'].inline_keyboard
        for button in row
    )


@pytest.mark.asyncio
async def test_cloudpayments_legacy_manual_entry_accepts_exact_displayed_minimum(monkeypatch):
    from app.handlers.balance import cloudpayments

    config = ProviderSettings()
    config.CLOUDPAYMENTS_MIN_AMOUNT_KOPEKS = 6407
    config.CLOUDPAYMENTS_MAX_AMOUNT_KOPEKS = 6407
    monkeypatch.setattr(cloudpayments, 'settings', config)
    create = AsyncMock(return_value=None)
    monkeypatch.setattr(cloudpayments, 'PaymentService', lambda: SimpleNamespace(create_cloudpayments_payment=create))
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_state(BalanceStates.waiting_for_amount)
    await state.set_data({'payment_method': 'cloudpayments'})
    message = MagicMock(spec=types.Message)
    message.text = '64.07'
    message.answer = AsyncMock()
    user = SimpleNamespace(id=1, telegram_id=1, language='ru', restriction_topup=False)
    db = AsyncMock()

    await inspect.unwrap(cloudpayments.process_cloudpayments_amount)(message, user, db, state)

    create.assert_awaited_once()
    assert create.await_args.kwargs['amount_kopeks'] == 6407
