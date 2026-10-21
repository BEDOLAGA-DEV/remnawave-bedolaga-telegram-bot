"""Selection changes while gift confirmation awaits external work."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.handlers.subscription import gift


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['data', 'state', 'none'])
@pytest.mark.parametrize('outcome', ['price', 'restricted', 'disabled', 'tariff', 'period', 'success', 'shortfall'])
async def test_confirmation_preserves_selection_changed_during_purchase(monkeypatch, changed, outcome):
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=42, user_id=42))
    selection = {
        'gift_checkout_id': 'original',
        'gift_tariff_id': 1,
        'gift_period_days': 30,
        'gift_expected_price_kopeks': 100,
    }
    await state.set_state('confirming')
    await state.set_data(selection)
    cart = {'cart_mode': 'gift_purchase', 'gift_checkout_id': 'original', '_cart_revision': 'old'}
    service = SimpleNamespace(
        get_user_cart=AsyncMock(return_value=cart),
        delete_processed_cart=AsyncMock(return_value=True),
        save_gift_cart_if_current=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(gift, 'user_cart_service', service)
    monkeypatch.setattr(gift, 'resolve_gift_claim_channel', AsyncMock(return_value=('test_bot', None)))
    monkeypatch.setattr(gift, '_render_confirmation_summary', lambda *args: ('confirmation', None))
    monkeypatch.setattr(gift, 'build_gift_result_presentation', lambda **kwargs: ('result', None))

    async def purchase(**kwargs):
        if changed == 'data':
            await state.set_data({'gift_checkout_id': 'new'})
        elif changed == 'state':
            await state.set_state('entering_gift_code')
        errors = {
            'price': gift.GiftPriceChangedError(100, SimpleNamespace(final_price_kopeks=200)),
            'restricted': gift.GiftPurchaseRestrictedError(),
            'disabled': gift.GiftFeatureDisabledError(),
            'tariff': gift.GiftTariffUnavailableError(),
            'period': gift.GiftPeriodUnavailableError(),
            'shortfall': gift.GiftInsufficientBalanceError(100, 50),
        }
        if outcome in errors:
            raise errors[outcome]
        return SimpleNamespace()

    monkeypatch.setattr(gift, 'purchase_gift_from_balance', purchase)
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock(), bot=AsyncMock())
    await gift.handle_gift_confirm(callback, SimpleNamespace(id=42, language='ru'), AsyncMock(), state)
    if changed != 'none':
        assert await state.get_data() == ({'gift_checkout_id': 'new'} if changed == 'data' else selection)
        assert await state.get_state() == ('entering_gift_code' if changed == 'state' else 'confirming')
        callback.message.edit_text.assert_not_awaited()
        service.save_gift_cart_if_current.assert_not_awaited()
    elif outcome == 'price':
        assert (await state.get_data())['gift_expected_price_kopeks'] == 200
        callback.message.edit_text.assert_awaited_once()
    elif outcome == 'shortfall':
        service.save_gift_cart_if_current.assert_awaited_once()
        callback.message.edit_text.assert_awaited_once()
    else:
        assert await state.get_state() is None
        assert await state.get_data() == {}
    if outcome == 'success':
        service.delete_processed_cart.assert_awaited_once_with(42, cart)


@pytest.mark.asyncio
async def test_success_does_not_clear_selection_started_during_result_delivery(monkeypatch):
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=42, user_id=42))
    await state.set_state('confirming')
    await state.set_data(
        {'gift_checkout_id': 'old', 'gift_tariff_id': 1, 'gift_period_days': 30, 'gift_expected_price_kopeks': 100}
    )
    monkeypatch.setattr(gift, 'user_cart_service', SimpleNamespace(get_user_cart=AsyncMock(return_value=None)))
    monkeypatch.setattr(gift, 'resolve_gift_claim_channel', AsyncMock(return_value=('test_bot', None)))
    monkeypatch.setattr(gift, 'purchase_gift_from_balance', AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(gift, 'build_gift_result_presentation', lambda **kwargs: ('result', None))

    async def deliver(*args, **kwargs):
        await state.set_state('new_selection')
        await state.set_data({'gift_checkout_id': 'new'})

    callback = SimpleNamespace(
        message=SimpleNamespace(edit_text=AsyncMock(side_effect=deliver)), answer=AsyncMock(), bot=AsyncMock()
    )
    await gift.handle_gift_confirm(callback, SimpleNamespace(id=42, language='ru'), AsyncMock(), state)
    assert await state.get_state() == 'new_selection'
    assert await state.get_data() == {'gift_checkout_id': 'new'}


@pytest.mark.asyncio
async def test_success_delivery_failure_keeps_checkout_for_idempotent_retry(monkeypatch):
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=42, user_id=42))
    selection = {
        'gift_checkout_id': 'old',
        'gift_tariff_id': 1,
        'gift_period_days': 30,
        'gift_expected_price_kopeks': 100,
    }
    await state.set_state('confirming')
    await state.set_data(selection)
    monkeypatch.setattr(gift, 'user_cart_service', SimpleNamespace(get_user_cart=AsyncMock(return_value=None)))
    monkeypatch.setattr(gift, 'resolve_gift_claim_channel', AsyncMock(return_value=('test_bot', None)))
    monkeypatch.setattr(gift, 'purchase_gift_from_balance', AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(gift, 'build_gift_result_presentation', lambda **kwargs: ('result', None))
    callback = SimpleNamespace(
        message=SimpleNamespace(edit_text=AsyncMock(side_effect=RuntimeError('delivery failed'))),
        answer=AsyncMock(),
        bot=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match='delivery failed'):
        await gift.handle_gift_confirm(callback, SimpleNamespace(id=42, language='ru'), AsyncMock(), state)
    assert await state.get_state() == 'confirming'
    assert await state.get_data() == selection
