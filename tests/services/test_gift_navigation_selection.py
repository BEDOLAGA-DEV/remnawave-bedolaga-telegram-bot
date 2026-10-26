"""Late catalog/navigation responses must not replace another gift selection."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.handlers.subscription import gift


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['state', 'data', 'both', 'none'])
@pytest.mark.parametrize(
    'action',
    [
        'catalog',
        'catalog_empty',
        'catalog_disabled',
        'catalog_disabled_history',
        'back_tariffs',
        'tariff',
        'tariff_missing',
        'back_periods',
        'back_periods_fallback',
    ],
)
async def test_gift_navigation_preserves_selection_changed_during_loading(monkeypatch, action, change):
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=42, user_id=42))
    data = {
        'gift_checkout_id': 'old',
        'gift_tariff_id': 1,
        'gift_period_days': 30,
        'gift_expected_price_kopeks': 100,
        'gift_origin_callback': 'menu_subscription',
        'gift_checkout_selection': [1, 30],
    }
    if action == 'back_periods_fallback':
        data.pop('gift_tariff_id')
    await state.set_state(gift.GiftPurchaseStates.confirming_purchase)
    await state.set_data(data)

    async def replace():
        if change in ('state', 'both'):
            await state.set_state('new_selection')
        if change in ('data', 'both'):
            await state.set_data({'gift_checkout_id': 'new', 'gift_tariff_id': 2})

    async def history(*args):
        if action.startswith('catalog_disabled'):
            await replace()
        return action == 'catalog_disabled_history'

    quote = gift.GiftQuote(1, 'Gift', 30, 100, 1, 100, 100, 0, 0, False)
    offer = gift.GiftTariffOffer(
        tariff_id=1,
        tariff_name='Gift',
        tariff_description=None,
        traffic_limit_gb=100,
        device_limit=1,
        display_order=1,
        quotes=(quote,),
    )

    async def offers(*args, **kwargs):
        await replace()
        return [] if action in ('catalog_empty', 'tariff_missing') else [offer]

    monkeypatch.setattr(gift, 'is_gift_enabled', AsyncMock(return_value=not action.startswith('catalog_disabled')))
    monkeypatch.setattr(gift, 'has_sender_gifts', history)
    monkeypatch.setattr(gift, 'list_gift_offers', offers)
    payload = {
        'tariff': 'gift_tariff:1',
        'tariff_missing': 'gift_tariff:1',
        'back_tariffs': 'gift_back_tariffs',
        'back_periods': 'gift_back_periods',
        'back_periods_fallback': 'gift_back_periods',
    }.get(action, 'subscription_gift')
    callback = SimpleNamespace(data=payload, message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    handler = {
        'tariff': gift.handle_gift_tariff_select,
        'tariff_missing': gift.handle_gift_tariff_select,
        'back_tariffs': gift.handle_gift_back_tariffs,
        'back_periods': gift.handle_gift_back_periods,
        'back_periods_fallback': gift.handle_gift_back_periods,
    }.get(action, gift.handle_gift_catalog)
    await handler(callback, SimpleNamespace(id=42, language='ru', balance_kopeks=1000), AsyncMock(), state)
    if change != 'none':
        assert await state.get_state() == (
            'new_selection' if change in ('state', 'both') else gift.GiftPurchaseStates.confirming_purchase.state
        )
        assert await state.get_data() == (
            {'gift_checkout_id': 'new', 'gift_tariff_id': 2} if change in ('data', 'both') else data
        )
        callback.message.edit_text.assert_not_awaited()
    else:
        callback.message.edit_text.assert_awaited_once()
        expected_state = gift.GiftPurchaseStates.confirming_purchase.state
        if action in ('catalog', 'back_tariffs', 'back_periods_fallback'):
            expected_state = gift.GiftPurchaseStates.selecting_tariff.state
        elif action in ('tariff', 'back_periods'):
            expected_state = gift.GiftPurchaseStates.selecting_period.state
        assert await state.get_state() == expected_state
        assert await state.get_data() == data


@pytest.mark.asyncio
async def test_back_periods_carries_original_snapshot_into_render_helper(monkeypatch):
    class ReplaceAfterReadStorage(MemoryStorage):
        armed = False

        async def get_data(self, key):
            result = await super().get_data(key)
            if self.armed:
                self.armed = False
                self.storage[key].state = 'new_selection'
                self.storage[key].data = {'gift_checkout_id': 'new', 'gift_tariff_id': 2}
            return result

    storage = ReplaceAfterReadStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=42, user_id=42))
    await state.set_state('old_selection')
    await state.set_data({'gift_checkout_id': 'old', 'gift_tariff_id': 1})
    storage.armed = True
    quote = gift.GiftQuote(1, 'Gift', 30, 100, 1, 100, 100, 0, 0, False)
    offer = gift.GiftTariffOffer(1, 'Gift', None, 100, 1, 1, (quote,))
    monkeypatch.setattr(gift, 'is_gift_enabled', AsyncMock(return_value=True))
    monkeypatch.setattr(gift, 'list_gift_offers', AsyncMock(return_value=[offer]))
    callback = SimpleNamespace(
        data='gift_back_periods', message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock()
    )
    await gift.handle_gift_back_periods(callback, SimpleNamespace(id=42, language='ru'), AsyncMock(), state)
    assert await state.get_state() == 'new_selection'
    assert await state.get_data() == {'gift_checkout_id': 'new', 'gift_tariff_id': 2}
    callback.message.edit_text.assert_not_awaited()
