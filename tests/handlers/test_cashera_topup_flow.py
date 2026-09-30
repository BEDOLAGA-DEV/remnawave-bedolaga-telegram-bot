"""Cashera в боте: кнопки способов пополнения и выбор метода — как у Platega."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import settings
from app.handlers.balance import cashera as cashera_handlers
from app.keyboards.inline import get_payment_methods_keyboard
from app.keyboards.topup_amounts import resolve_config_method_id
from app.states import BalanceStates


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    for key, value in {
        'CASHERA_ENABLED': True,
        'CASHERA_API_KEY': 'pk_test',
        'CASHERA_API_SECRET': 'sk_test',
        'CASHERA_ACTIVE_METHODS': 'sbp,card',
        'CASHERA_INLINE_METHODS': False,
    }.items():
        monkeypatch.setattr(settings, key, value, raising=False)


def _callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]


def test_single_button_by_default():
    assert 'topup_cashera' in _callbacks(get_payment_methods_keyboard(0, 'ru'))


def test_inline_methods_show_one_button_per_method(monkeypatch):
    monkeypatch.setattr(settings, 'CASHERA_INLINE_METHODS', True, raising=False)
    callbacks = _callbacks(get_payment_methods_keyboard(0, 'ru'))
    assert 'topup_cashera_m_sbp' in callbacks
    assert 'topup_cashera_m_card' in callbacks
    assert 'topup_cashera' not in callbacks


def test_prefilled_amount_keeps_method_in_callback(monkeypatch):
    monkeypatch.setattr(settings, 'CASHERA_INLINE_METHODS', True, raising=False)
    assert 'topup_amount|cashera_m_sbp|50000' in _callbacks(get_payment_methods_keyboard(50000, 'ru'))


def test_quick_amounts_resolve_to_cashera_config():
    assert resolve_config_method_id('cashera') == 'cashera'
    assert resolve_config_method_id('cashera_m_card') == 'cashera'


def test_hidden_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, 'CASHERA_ENABLED', False, raising=False)
    assert not [c for c in _callbacks(get_payment_methods_keyboard(0, 'ru')) if 'cashera' in c]


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=7, user_id=7))


def _callback(data: str):
    message = MagicMock()
    message.edit_text = AsyncMock()
    message.message_id = 10
    message.chat = SimpleNamespace(id=7)
    return SimpleNamespace(data=data, message=message, answer=AsyncMock())


def _user():
    return SimpleNamespace(id=7, telegram_id=7, language='ru', restriction_topup=False)


@pytest.mark.asyncio
async def test_several_methods_ask_to_choose(monkeypatch):
    state = _state()
    callback = _callback('topup_cashera')

    await cashera_handlers.start_cashera_payment(callback, _user(), state)

    markup = callback.message.edit_text.call_args.kwargs['reply_markup']
    assert _callbacks(markup)[:2] == ['cashera_method_sbp', 'cashera_method_card']
    assert await state.get_state() == BalanceStates.waiting_for_cashera_method.state


@pytest.mark.asyncio
async def test_choosing_a_method_asks_for_amount(monkeypatch):
    monkeypatch.setattr(cashera_handlers, 'get_topup_amount_keyboard', AsyncMock(return_value=None))
    state = _state()

    await cashera_handlers.handle_cashera_method_selection(_callback('cashera_method_card'), _user(), state)

    data = await state.get_data()
    assert data['payment_method'] == 'cashera'
    assert data['cashera_method'] == 'card'
    assert await state.get_state() == BalanceStates.waiting_for_amount.state


@pytest.mark.asyncio
async def test_disabled_method_is_refused(monkeypatch):
    callback = _callback('cashera_method_crypto')
    await cashera_handlers.handle_cashera_method_selection(callback, _user(), _state())
    callback.answer.assert_awaited_once()
    assert callback.answer.call_args.kwargs.get('show_alert') is True


class _FakePaymentService:
    h2h = None

    def __init__(self, _bot=None):
        pass

    async def create_cashera_payment(self, **_kwargs):
        return {'payment_url': 'https://pay.cashera.cash/x', 'payment_id': 'u-1', 'local_payment_id': 5}

    async def get_cashera_h2h(self, _uuid, _method):
        return self.h2h


def _amount_message():
    message = MagicMock()
    message.answer = AsyncMock()
    message.answer_photo = AsyncMock()
    message.delete = AsyncMock()
    message.chat = SimpleNamespace(id=7)
    message.bot = MagicMock()
    return message


@pytest.mark.asyncio
@pytest.mark.parametrize(('h2h', 'photo'), [({'qr': 'https://qr.nspk.ru/AS1'}, True), (None, False)])
async def test_invoice_is_a_qr_photo_when_requisites_are_ready(monkeypatch, h2h, photo):
    _FakePaymentService.h2h = h2h
    monkeypatch.setattr(cashera_handlers, 'PaymentService', _FakePaymentService)
    state = _state()
    await state.update_data(cashera_method='sbp')
    message = _amount_message()

    await cashera_handlers.process_cashera_payment_amount(message, _user(), object(), 50000, state)

    assert message.answer_photo.await_count == (1 if photo else 0)
    assert message.answer.await_count == (0 if photo else 1)
    sent = message.answer_photo if photo else message.answer
    buttons = sent.call_args.kwargs['reply_markup'].inline_keyboard
    assert buttons[0][0].url == 'https://pay.cashera.cash/x'  # ссылка остаётся запасным путём
