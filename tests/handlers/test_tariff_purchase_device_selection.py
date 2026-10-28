from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.handlers.subscription import tariff_purchase as telegram
from tests.services.test_tariff_purchase_quote import make_tariff


@pytest.fixture(autouse=True)
def no_conversion_candidate(monkeypatch):
    monkeypatch.setattr(
        'app.database.crud.subscription.resolve_trial_conversion_candidate', AsyncMock(return_value=None)
    )


def context():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=101, user_id=101))


def user():
    return SimpleNamespace(id=1, language='ru', balance_kopeks=100000, get_primary_promo_group=lambda: None)


async def bind(state, tariff=None, subscription=None):
    return await telegram._bind_tariff_purchase_keyboard(
        telegram.get_tariff_confirm_keyboard(7, 30, 'ru'),
        tariff=tariff or make_tariff(),
        period=30,
        db_user=user(),
        db=AsyncMock(),
        state=state,
        fixed=True,
        subscription=subscription,
    )


async def test_new_purchase_keyboard_offers_device_selection():
    state = context()
    keyboard = await bind(state)
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert any((button.callback_data or '').startswith('tariff_devices:') for button in buttons)
    assert all(len((button.callback_data or '').encode()) <= 64 for button in buttons)


async def test_selected_devices_bound_to_exact_cart_terms():
    state = context()
    await state.update_data(tariff_device_selection={'tariff_id': 7, 'limit': 3})
    await bind(state)
    cart = (await state.get_data())['restored_tariff_checkout']['cart']
    assert cart['selected_device_limit'] == 3
    assert cart['device_quote_terms']['pricing']['final_total'] == 50000
    assert cart['device_parameters']['selected_device_limit'] == 3


async def test_selection_does_not_leak_into_another_tariff():
    state = context()
    await state.update_data(tariff_device_selection={'tariff_id': 8, 'limit': 3})
    await bind(state)
    assert 'selected_device_limit' not in (await state.get_data())['restored_tariff_checkout']['cart']


async def test_paid_renewal_has_no_device_selector():
    state = context()
    subscription = SimpleNamespace(id=10, tariff_id=7, device_limit=4, is_trial=False)
    keyboard = await bind(state, subscription=subscription)
    assert not any(
        (button.callback_data or '').startswith('tariff_devices:') for row in keyboard.inline_keyboard for button in row
    )


async def test_selected_extra_devices_do_not_expose_incompatible_recurring_checkout(monkeypatch):
    monkeypatch.setattr(type(telegram.settings), 'is_platega_recurrent_enabled', lambda self: True)
    state = context()
    await state.update_data(tariff_device_selection={'tariff_id': 7, 'limit': 3})
    keyboard = await bind(state)
    assert not any(
        (button.callback_data or '').startswith(('tariff_sbp:', 'tariff_lava:', 'tariff_cashera:'))
        for row in keyboard.inline_keyboard
        for button in row
    )


async def test_custom_preview_includes_selected_devices_in_price(monkeypatch):
    state = context()
    await state.update_data(tariff_device_selection={'tariff_id': 7, 'limit': 3})
    monkeypatch.setattr(telegram, '_resolve_purchase_subscription', AsyncMock(return_value=None))
    tariff = make_tariff(name='Test')
    text = await telegram.format_custom_tariff_preview(
        tariff, 30, 100, 100000, db_user=user(), db=AsyncMock(), state=state
    )
    assert 'Устройств: 3' in text
    assert '500' in text


@pytest.mark.parametrize('stale', [False, True])
async def test_device_callback_is_bound_to_current_attempt(monkeypatch, stale):
    import inspect

    state = context()
    keyboard = await bind(state)
    button = next(
        button
        for row in keyboard.inline_keyboard
        for button in row
        if (button.callback_data or '').startswith('tariff_devices:')
    )
    callback = SimpleNamespace(data=button.callback_data, answer=AsyncMock())
    callback.model_copy = lambda update: SimpleNamespace(**(vars(callback) | update))
    monkeypatch.setattr(telegram, 'get_tariff_by_id', AsyncMock(return_value=make_tariff(is_active=True)))
    monkeypatch.setattr(telegram, '_reject_unavailable_tariff', AsyncMock(return_value=False))
    monkeypatch.setattr(telegram, '_resolve_purchase_subscription', AsyncMock(return_value=None))
    redraw = AsyncMock()
    monkeypatch.setattr(telegram, 'select_tariff_period', redraw)
    if stale:
        await bind(state)
    await inspect.unwrap(telegram.handle_tariff_devices_change)(callback, user(), AsyncMock(), state)
    if stale:
        redraw.assert_not_awaited()
        assert 'tariff_device_selection' not in await state.get_data()
    else:
        redraw.assert_awaited_once()
        assert (await state.get_data())['tariff_device_selection']['limit'] == 2


@pytest.mark.parametrize('text', ['1.5', '-1', '1e3', '100000000000', 'bad'])
async def test_manual_input_rejects_noninteger_without_dispatch(monkeypatch, text):
    state = context()
    await bind(state)
    await state.update_data(device_input_token='test')
    apply = AsyncMock()
    monkeypatch.setattr(telegram, 'handle_tariff_devices_change', apply)
    message = SimpleNamespace(text=text, answer=AsyncMock(), bot=None, from_user=None)
    await telegram.receive_tariff_device_limit(message, user(), AsyncMock(), state)
    apply.assert_not_awaited()
    message.answer.assert_awaited_once()


async def test_manual_input_accepts_large_integer_and_reuses_validated_callback(monkeypatch):
    state = context()
    await bind(state)
    await state.update_data(device_input_token='token')
    apply = AsyncMock()
    monkeypatch.setattr(telegram, 'handle_tariff_devices_change', apply)
    message = SimpleNamespace(text='150', answer=AsyncMock(), bot=None, from_user=None)
    await telegram.receive_tariff_device_limit(message, user(), AsyncMock(), state)
    assert apply.await_args.args[0].data == 'tariff_devices:7:150:token'
