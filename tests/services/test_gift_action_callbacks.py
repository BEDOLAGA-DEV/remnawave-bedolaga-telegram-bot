"""Old gift messages must not act on the current checkout or quote."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.handlers.subscription import gift


def action_payload(action, data, state):
    """Versioned callback contract, independent of handler admission checks."""
    fields = [
        data.get(key)
        for key in (
            'gift_checkout_id',
            'gift_tariff_id',
            'gift_period_days',
            'gift_expected_price_kopeks',
            'gift_origin_callback',
        )
    ]
    encoded = json.dumps([*fields, state], ensure_ascii=False, separators=(',', ':'))
    return f'{action}:{hashlib.sha256(encoded.encode()).hexdigest()[:24]}'


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['gift_confirm', 'gift_cancel'])
@pytest.mark.parametrize('change', ['checkout', 'tariff', 'period', 'price', 'state', 'legacy', 'malformed', 'none'])
async def test_old_gift_action_cannot_purchase_or_cancel_current_selection(monkeypatch, action, change):
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=42, user_id=42))
    data = {
        'gift_checkout_id': 'old',
        'gift_tariff_id': 1,
        'gift_period_days': 30,
        'gift_expected_price_kopeks': 100,
        'gift_origin_callback': 'menu_subscription',
    }
    original_state = gift.GiftPurchaseStates.confirming_purchase.state
    payload = action_payload(action, data, original_state)
    if change == 'legacy':
        payload = action
    elif change == 'malformed':
        payload = action + ':invalid'
    values = {
        'checkout': ('gift_checkout_id', 'new'),
        'tariff': ('gift_tariff_id', 2),
        'period': ('gift_period_days', 90),
        'price': ('gift_expected_price_kopeks', 200),
    }
    current = {**data}
    if change in values:
        field, value = values[change]
        current[field] = value
    current_state = gift.GiftPurchaseStates.selecting_period.state if change == 'state' else original_state
    await state.set_data(current)
    await state.set_state(current_state)
    cart = {'cart_mode': 'gift_purchase', 'gift_checkout_id': current['gift_checkout_id']}
    service = SimpleNamespace(
        get_user_cart=AsyncMock(return_value=cart), delete_processed_cart=AsyncMock(return_value=True)
    )
    purchase = AsyncMock(return_value=SimpleNamespace())
    show_origin = AsyncMock()
    monkeypatch.setattr(gift, 'user_cart_service', service)
    monkeypatch.setattr(gift, 'purchase_gift_from_balance', purchase)
    monkeypatch.setattr(gift, 'resolve_gift_claim_channel', AsyncMock(return_value=('test_bot', None)))
    monkeypatch.setattr(gift, 'build_gift_result_presentation', lambda **kwargs: ('done', None))
    monkeypatch.setattr(gift, 'show_subscription_info', show_origin)
    callback = SimpleNamespace(
        data=payload, message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock(), bot=AsyncMock()
    )
    handler = gift.handle_gift_confirm if action == 'gift_confirm' else gift.handle_gift_cancel
    await handler(callback, SimpleNamespace(id=42, language='ru'), AsyncMock(), state)
    if change != 'none':
        purchase.assert_not_awaited()
        service.delete_processed_cart.assert_not_awaited()
        show_origin.assert_not_awaited()
        callback.message.edit_text.assert_not_awaited()
        assert await state.get_data() == current
        assert await state.get_state() == current_state
        callback.answer.assert_awaited_once()
    else:
        assert await state.get_data() == {}
        assert await state.get_state() is None
        service.delete_processed_cart.assert_awaited_once_with(42, cart)
        if action == 'gift_confirm':
            purchase.assert_awaited_once()
            assert purchase.await_args.kwargs['expected_price_kopeks'] == 100
            assert purchase.await_args.kwargs['idempotency_key'] == 'old'
        else:
            show_origin.assert_awaited_once()
    assert len(payload.encode()) <= 64


def test_bound_keyboard_keeps_labels_navigation_and_telegram_callback_limit():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text='Confirm', callback_data='gift_confirm'),
                InlineKeyboardButton(text='Cancel', callback_data='gift_cancel'),
                InlineKeyboardButton(text='Back', callback_data='gift_back_periods'),
            ]
        ]
    )
    selection = {
        'gift_checkout_id': 'a' * 128,
        'gift_tariff_id': 123,
        'gift_period_days': 365,
        'gift_expected_price_kopeks': 123456789,
    }
    bound = gift._bind_gift_actions(keyboard, selection, gift.GiftPurchaseStates.confirming_purchase.state)
    assert [button.text for button in bound.inline_keyboard[0]] == ['Confirm', 'Cancel', 'Back']
    assert [button.callback_data for button in keyboard.inline_keyboard[0]] == [
        'gift_confirm',
        'gift_cancel',
        'gift_back_periods',
    ]
    for button in bound.inline_keyboard[0][:2]:
        assert len(button.callback_data.encode()) <= 64
        action = button.callback_data.split(':')[0]
        assert button.callback_data == action_payload(
            action, selection, gift.GiftPurchaseStates.confirming_purchase.state
        )
    assert bound.inline_keyboard[0][2].callback_data == 'gift_back_periods'
