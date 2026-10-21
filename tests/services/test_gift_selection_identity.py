"""Gift selection identity must survive retries but not move to another quote."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.handlers.subscription import gift


@pytest.mark.asyncio
@pytest.mark.parametrize('selection', ['tariff', 'period', 'legacy', 'same', 'initial', 'superseded'])
async def test_period_selection_uses_its_own_checkout_and_preserves_newer_fsm(monkeypatch, selection):
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=42, user_id=42))
    data = {
        'gift_checkout_id': 'original',
        'gift_tariff_id': 2 if selection == 'tariff' else 1,
        'gift_period_days': 30,
        'gift_expected_price_kopeks': 100,
    }
    if selection == 'initial':
        data.pop('gift_period_days')
        data.pop('gift_expected_price_kopeks')
    if selection not in ('legacy', 'initial'):
        # Navigation may already have changed gift_tariff_id; the checkout's
        # own identity must still refer to its original tariff/period.
        data['gift_checkout_selection'] = [1, 30]
    await state.set_data(data)
    await state.set_state(gift.GiftPurchaseStates.selecting_period)
    tariff_id = 2 if selection == 'tariff' else 1
    days = 90 if selection == 'period' else 30
    quote = gift.GiftQuote(tariff_id, 'Gift', days, 100, 1, 100, 100, 0, 0, False)

    async def requote(*args, **kwargs):
        if selection == 'superseded':
            await state.set_state('new_selection')
            await state.set_data({'gift_checkout_id': 'new'})
        return quote

    monkeypatch.setattr(gift, 'quote_gift_purchase', requote)
    monkeypatch.setattr(gift, 'user_cart_service', SimpleNamespace(get_user_cart=AsyncMock(return_value=None)))
    callback = SimpleNamespace(
        data=f'gift_period:{tariff_id}:{days}', message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock()
    )
    await gift.handle_gift_period_select(
        callback, SimpleNamespace(id=42, language='ru', balance_kopeks=1000), AsyncMock(), state
    )
    result = await state.get_data()
    if selection == 'superseded':
        assert result == {'gift_checkout_id': 'new'}
        assert await state.get_state() == 'new_selection'
        callback.message.edit_text.assert_not_awaited()
    else:
        assert result['gift_checkout_selection'] == [tariff_id, days]
        assert (result['gift_checkout_id'] == 'original') is (selection in ('same', 'initial'))
        assert await state.get_state() == gift.GiftPurchaseStates.confirming_purchase.state
        keyboard = callback.message.edit_text.await_args.kwargs['reply_markup']
        expected = gift._gift_action_callback('gift_confirm', result, await state.get_state())
        assert expected in [button.callback_data for row in keyboard.inline_keyboard for button in row]
