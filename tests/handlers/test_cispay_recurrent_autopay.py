from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.subscription import cispay_recurrent as handler


def context(data):
    return (
        SimpleNamespace(data=data, answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock())),
        SimpleNamespace(id=1, language='ru'),
        AsyncMock(),
        AsyncMock(),
    )


async def test_stale_consent_never_pays(monkeypatch):
    callback, user, db, state = context('cisrec:pay:old')
    state.get_data.return_value = {'cispay_consent': {'token': 'new'}}
    purchase = AsyncMock()
    monkeypatch.setattr(handler, 'purchase_cispay_quote', purchase)
    await handler.handle_cispay_recurrent(callback, user, db, state)
    purchase.assert_not_called()
    callback.answer.assert_awaited_once()


async def test_cancel_checks_ownership_before_provider(monkeypatch):
    callback, user, db, state = context('cisrec:cancel:2')
    db.scalar.return_value = None
    cancel = AsyncMock()
    monkeypatch.setattr(handler, 'cancel_cispay_agreement', cancel)
    await handler.handle_cispay_recurrent(callback, user, db, state)
    cancel.assert_not_called()


async def test_payment_retry_uses_same_checkout(monkeypatch):
    callback, user, db, state = context('cisrec:pay:token')
    state.get_data.return_value = {'cispay_consent': {'token': 'token', 'quote_id': 'quote', 'checkout_id': 'checkout'}}
    purchase = AsyncMock(return_value={'subscription_id': 2, 'status': 'CREATION_UNKNOWN'})
    monkeypatch.setattr(handler, 'purchase_cispay_quote', purchase)
    monkeypatch.setattr(handler, 'get_cispay_agreement_state', AsyncMock(return_value={'status': 'CREATION_UNKNOWN'}))
    await handler.handle_cispay_recurrent(callback, user, db, state)
    await handler.handle_cispay_recurrent(callback, user, db, state)
    assert purchase.await_count == 2
    assert all(call.kwargs['checkout_id'] == 'checkout' for call in purchase.await_args_list)


async def test_show_quote_never_pays_before_confirmation(monkeypatch):
    callback, user, db, state = context('tariff_cispay:3')
    state.get_data.return_value = {
        'selected_tariff_id': 3,
        'selected_period': 10,
        'subscription_id': 2,
        'tariff_device_selection': {'tariff_id': 3, 'limit': 0},
    }
    quote = AsyncMock(return_value={'quote_id': 'quote', 'amount_kopeks': 12300, 'period_days': 10})
    purchase = AsyncMock()
    monkeypatch.setattr(handler, 'quote_cispay_purchase', quote)
    monkeypatch.setattr(handler, 'purchase_cispay_quote', purchase)
    await handler.purchase_cispay_from_tariff(callback, user, db, state)
    assert quote.await_args.kwargs['subscription_id'] == 2
    assert quote.await_args.kwargs['device_limit'] == 0
    assert quote.await_args.kwargs['period_days'] == 10
    purchase.assert_not_called()
    assert '123' in callback.message.edit_text.await_args.args[0]


def test_trial_keyboard_retains_cispay_management_after_leaving_checkout():
    from app.keyboards.inline import get_subscription_keyboard

    keyboard = get_subscription_keyboard(
        'ru',
        has_subscription=True,
        is_trial=True,
        subscription=SimpleNamespace(id=42, subscription_url=None, subscription_crypto_link=None),
        cispay_recurring=True,
    )
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert 'cisrec:menu:42' in callbacks


def test_status_message_contains_price_period_date_and_reason():
    from app.localization.texts import get_texts

    message = handler.format_cispay_status(
        {
            'status': 'CANCEL_PENDING',
            'amount_kopeks': 12300,
            'period_days': 10,
            'next_charge_at': '2026-12-01T10:00:00Z',
            'reason': 'price_changed',
        },
        get_texts('ru'),
    )
    assert '123' in message and '10' in message and '01.12.2026' in message
    assert 'цен' in message.lower()


async def test_real_bound_cart_uses_effective_devices_and_rejects_old_screen(monkeypatch):
    from app.services.subscription_auto_purchase_service import _saved_tariff_checkout_external_id

    cart = {
        'tariff_id': 3,
        'period_days': 10,
        'subscription_id': 42,
        'selected_device_limit': 5,
        'tariff_checkout_id': 'new',
    }
    token = _saved_tariff_checkout_external_id(1, cart).rsplit(':', 1)[-1][:24]
    callback, user, db, state = context(f'tariff_cispay:3:{token}')
    state.get_data.return_value = {'restored_tariff_checkout': {'cart': cart, 'identity': cart}}
    quote = AsyncMock(return_value={'quote_id': 'quote', 'amount_kopeks': 12300, 'period_days': 10})
    monkeypatch.setattr(handler, 'quote_cispay_purchase', quote)
    await handler.purchase_cispay_from_tariff(callback, user, db, state)
    assert quote.await_args.kwargs['subscription_id'] == 42
    assert quote.await_args.kwargs['device_limit'] == 5
    quote.reset_mock()
    callback.data = 'tariff_cispay:3:previous-screen'
    await handler.purchase_cispay_from_tariff(callback, user, db, state)
    quote.assert_not_called()


def test_checkout_buttons_respect_exact_period(monkeypatch):
    from app.config import settings
    from app.handlers.subscription import tariff_purchase as purchase

    monkeypatch.setattr(settings, 'CISPAY_RECURRENT_ENABLED', True)
    monkeypatch.setattr(type(settings), 'is_cispay_sbp_enabled', lambda self: True)

    def has_button(keyboard):
        return any(
            (button.callback_data or '').startswith('tariff_cispay:')
            for row in keyboard.inline_keyboard
            for button in row
        )

    assert has_button(purchase.get_tariff_confirm_keyboard(1, 30, 'ru'))
    for days in (1, 6, 361, 365, 10000):
        assert not has_button(purchase.get_tariff_insufficient_balance_keyboard(1, days, 'ru'))
    assert not has_button(purchase.get_daily_tariff_insufficient_balance_keyboard(1, 'ru'))


async def test_trial_management_quote_uses_tariff_default_devices(monkeypatch):
    callback, user, db, state = context('cisrec:quote:42:30')
    db.scalar.return_value = SimpleNamespace(id=42, tariff_id=3, is_trial=True, device_limit=1)
    show = AsyncMock()
    monkeypatch.setattr(handler, '_show_quote', show)
    await handler.handle_cispay_recurrent(callback, user, db, state)
    assert show.await_args.kwargs['device_limit'] is None


def test_unconnected_status_is_not_reported_as_still_loading():
    from app.localization.texts import get_texts

    assert 'Проверяем' not in handler.format_cispay_status({'status': 'none'}, get_texts('ru'))


async def test_custom_period_navigation_keeps_owned_target(monkeypatch):
    from app.states import SubscriptionStates

    callback, user, db, state = context('cisrec:select:42')
    db.scalar.return_value = SimpleNamespace(id=42, tariff_id=3, status='active')
    db.get.return_value = custom_tariff()
    monkeypatch.setattr(handler, 'get_cispay_recurrent_policy', AsyncMock(return_value={'enabled': True}))
    await handler.handle_cispay_recurrent(callback, user, db, state)
    state.update_data.assert_awaited_once_with(cispay_period_subscription_id=42, cispay_consent=None)
    state.set_state.assert_awaited_once_with(SubscriptionStates.entering_cispay_period)
    assert '7' in callback.message.edit_text.await_args.args[0]
    assert '360' in callback.message.edit_text.await_args.args[0]


def custom_tariff(**changes):
    values = dict(
        is_active=True,
        is_daily=False,
        min_days=1,
        max_days=10000,
        get_available_periods=lambda: [30],
        can_purchase_custom_days=lambda: True,
    )
    return SimpleNamespace(**(values | changes))


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('trial', [False, True])
@pytest.mark.parametrize('days', [7, 30, 360])
async def test_custom_period_quotes_owned_target_without_new_purchase(monkeypatch, multi, trial, days):
    from app.config import settings

    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: multi)
    _, user, db, state = context('unused')
    state.get_data.return_value = {'cispay_period_subscription_id': 42}
    db.scalar.return_value = SimpleNamespace(id=42, tariff_id=3, is_trial=trial, device_limit=5)
    db.get.return_value = custom_tariff()
    message = SimpleNamespace(text=str(days), answer=AsyncMock())
    quote = AsyncMock(return_value={'quote_id': 'quote', 'amount_kopeks': 12300, 'period_days': days})
    pay = AsyncMock()
    monkeypatch.setattr(handler, 'quote_cispay_purchase', quote)
    monkeypatch.setattr(handler, 'purchase_cispay_quote', pay)
    await handler.process_cispay_period(message, user, db, state)
    assert quote.await_args.kwargs == dict(
        user=user, channel='telegram', tariff_id=3, period_days=days, device_limit=None, subscription_id=42
    )
    pay.assert_not_awaited()
    state.set_state.assert_awaited_once_with(None)
    assert '123' in message.answer.await_args.args[0]


@pytest.mark.parametrize('value', [None, '', '3', '361', '10000', '7.5', '1e2', '-30'])
async def test_invalid_custom_days_never_quote(monkeypatch, value):
    _, user, db, state = context('unused')
    state.get_data.return_value = {'cispay_period_subscription_id': 42}
    db.scalar.return_value = SimpleNamespace(id=42, tariff_id=3)
    db.get.return_value = custom_tariff()
    quote = AsyncMock()
    monkeypatch.setattr(handler, 'quote_cispay_purchase', quote)
    message = SimpleNamespace(text=value, answer=AsyncMock())
    await handler.process_cispay_period(message, user, db, state)
    quote.assert_not_awaited()
    state.set_state.assert_not_awaited()
    message.answer.assert_awaited_once()


async def test_custom_days_rechecks_ownership(monkeypatch):
    _, user, db, state = context('unused')
    state.get_data.return_value = {'cispay_period_subscription_id': 42}
    db.scalar.return_value = None
    quote = AsyncMock()
    monkeypatch.setattr(handler, 'quote_cispay_purchase', quote)
    await handler.process_cispay_period(SimpleNamespace(text='30', answer=AsyncMock()), user, db, state)
    quote.assert_not_awaited()
    statement = str(db.scalar.await_args.args[0])
    assert 'subscriptions.user_id =' in statement


async def test_back_leaves_custom_input_state(monkeypatch):
    from app.states import SubscriptionStates

    callback, user, db, state = context('cisrec:menu:42')
    state.get_state.return_value = SubscriptionStates.entering_cispay_period.state
    db.scalar.return_value = SimpleNamespace(id=42)
    monkeypatch.setattr(handler, 'get_cispay_agreement_state', AsyncMock(return_value={'status': 'ACTIVE'}))
    await handler.handle_cispay_recurrent(callback, user, db, state)
    state.set_state.assert_awaited_once_with(None)


@pytest.mark.parametrize('kind', ['disabled', 'daily', 'inactive', 'no-custom', 'out-of-range', 'off'])
async def test_unavailable_custom_setup_never_opens_input(monkeypatch, kind):
    callback, user, db, state = context('cisrec:select:42')
    db.scalar.return_value = SimpleNamespace(id=42, tariff_id=3, status='disabled' if kind == 'disabled' else 'active')
    db.get.return_value = custom_tariff(
        is_daily=kind == 'daily',
        is_active=kind != 'inactive',
        min_days=400 if kind == 'out-of-range' else 1,
        can_purchase_custom_days=lambda: kind != 'no-custom',
    )
    monkeypatch.setattr(handler, 'get_cispay_recurrent_policy', AsyncMock(return_value={'enabled': kind != 'off'}))
    await handler.handle_cispay_recurrent(callback, user, db, state)
    state.set_state.assert_not_awaited()
    assert callback.answer.await_args.kwargs['show_alert'] is True
