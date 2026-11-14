from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import Tariff
from app.handlers.subscription import payhot_recurrent as handler
from app.localization.texts import get_texts


def context(data):
    return (
        SimpleNamespace(data=data, answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock())),
        SimpleNamespace(id=1, language='ru'),
        AsyncMock(),
        AsyncMock(),
    )


async def test_stale_consent_never_pays(monkeypatch):
    callback, user, db, state = context('hotrec:pay:old')
    state.get_data.return_value = {'payhot_consent': {'token': 'new'}}
    purchase = AsyncMock()
    monkeypatch.setattr(handler, 'purchase_payhot_quote', purchase)
    await handler.handle_payhot_recurrent(callback, user, db, state)
    purchase.assert_not_called()


@pytest.mark.parametrize('action', ['cancel', 'menu', 'quote'])
async def test_foreign_target_rejected_before_provider(monkeypatch, action):
    callback, user, db, state = context(f'hotrec:{action}:2:30')
    db.scalar.return_value = None
    cancel = AsyncMock()
    quote = AsyncMock()
    get = AsyncMock()
    monkeypatch.setattr(handler, 'cancel_payhot_agreement', cancel)
    monkeypatch.setattr(handler, 'quote_payhot_purchase', quote)
    monkeypatch.setattr(handler, 'get_payhot_agreement_state', get)
    await handler.handle_payhot_recurrent(callback, user, db, state)
    cancel.assert_not_called()
    quote.assert_not_called()
    get.assert_not_called()


async def test_unknown_retry_reuses_checkout(monkeypatch):
    callback, user, db, state = context('hotrec:pay:token')
    state.get_data.return_value = {'payhot_consent': {'token': 'token', 'quote_id': 'quote', 'checkout_id': 'checkout'}}
    purchase = AsyncMock(return_value={'subscription_id': 2})
    monkeypatch.setattr(handler, 'purchase_payhot_quote', purchase)
    monkeypatch.setattr(handler, 'get_payhot_agreement_state', AsyncMock(return_value={'status': 'CREATION_UNKNOWN'}))
    for _ in range(2):
        await handler.handle_payhot_recurrent(callback, user, db, state)
    assert purchase.await_count == 2
    assert all(c.kwargs['checkout_id'] == 'checkout' for c in purchase.await_args_list)
    assert 'ещё' not in callback.message.edit_text.await_args.args[0]


@pytest.mark.parametrize('target', [None, 2])
async def test_method_selection_bound_to_fsm_and_owned_target(monkeypatch, target):
    callback, user, db, state = context(f'hotrec:method:{target or 0}:sbp:token')
    state.get_data.return_value = {
        'payhot_selection': {
            'subscription_id': target,
            'tariff_id': 3,
            'period_days': 1000,
            'device_limit': 0,
            'token': 'token',
        }
    }
    db.scalar.return_value = target
    quote = AsyncMock(
        return_value={
            'quote_id': 'quote',
            'amount_kopeks': 10000,
            'period_days': 1000,
            'device_limit': 0,
            'tariff_id': 3,
            'source_subscription_id': target,
            'payment_method': 'sbp',
        }
    )
    db.get.return_value = Tariff(id=3, name='Test tariff')
    monkeypatch.setattr(handler, 'quote_payhot_purchase', quote)
    await handler.handle_payhot_recurrent(callback, user, db, state)
    assert quote.await_args.kwargs['payment_method'] == 'sbp'
    assert quote.await_args.kwargs['device_limit'] == 0
    assert quote.await_args.kwargs['subscription_id'] == target


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'fa', 'zh'])
@pytest.mark.parametrize('device_limit', [0, 7])
@pytest.mark.parametrize('target', [None, 42])
@pytest.mark.parametrize('daily', [False, True])
async def test_consent_identifies_quoted_target_tariff_and_devices(monkeypatch, language, device_limit, target, daily):
    callback, user, db, state = context('unused')
    user.language = language
    db.get.return_value = Tariff(id=3, name='<b>Family & friends</b>')
    quote = {
        'quote_id': 'quote',
        'amount_kopeks': 12300,
        'period_days': 1 if daily else 30,
        'device_limit': device_limit,
        'tariff_id': 3,
        'source_subscription_id': target,
        'payment_method': 'sbp',
        'is_daily': daily,
    }
    monkeypatch.setattr(handler, 'quote_payhot_purchase', AsyncMock(return_value=quote))
    await handler._present_quote(callback.message.edit_text, user, db, state, device_limit=99)
    message = callback.message.edit_text.await_args.args[0]
    texts = get_texts(language)
    assert '&lt;b&gt;Family &amp; friends&lt;/b&gt;' in message
    assert '<b>Family' not in message
    devices = texts.t('HOTREC_UNLIMITED', 'MISSING') if device_limit == 0 else '7'
    assert texts.t('HOTREC_DEVICES', 'MISSING').format(devices=devices) in message
    target_text = texts.t('HOTREC_TARGET_EXISTING', 'MISSING') if target else texts.t('HOTREC_TARGET_NEW', 'MISSING')
    assert target_text != 'MISSING' and target_text in message
    assert texts.t('HOTREC_METHOD_SBP') in message
    assert '99' not in message


async def test_foreign_method_target_never_quotes(monkeypatch):
    callback, user, db, state = context('hotrec:method:2:card:token')
    state.get_data.return_value = {'payhot_selection': {'subscription_id': 2, 'token': 'token'}}
    db.scalar.return_value = None
    quote = AsyncMock()
    monkeypatch.setattr(handler, 'quote_payhot_purchase', quote)
    await handler.handle_payhot_recurrent(callback, user, db, state)
    quote.assert_not_called()


async def test_flag_off_keeps_pending_cancel_visible(monkeypatch):
    callback, user, db, state = context('hotrec:menu:2')
    db.scalar.return_value = SimpleNamespace(id=2, tariff_id=None)
    monkeypatch.setattr(handler, 'get_payhot_agreement_state', AsyncMock(return_value={'status': 'CANCEL_PENDING'}))
    monkeypatch.setattr(handler, 'get_payhot_recurrent_policy', AsyncMock(return_value={'enabled': False}))
    await handler.handle_payhot_recurrent(callback, user, db, state)
    rows = callback.message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
    assert 'hotrec:cancel:2' in [b.callback_data for r in rows for b in r]


@pytest.mark.parametrize('value', ['0', '1.0', '-1', '10001', '１２３.0'])
async def test_custom_period_strict_integer(monkeypatch, value):
    message = SimpleNamespace(text=value, answer=AsyncMock())
    user = SimpleNamespace(id=1, language='ru')
    db = AsyncMock()
    state = AsyncMock()
    state.get_data.return_value = {'payhot_period_subscription_id': 2}
    db.scalar.return_value = SimpleNamespace(id=2, tariff_id=3)
    db.get.return_value = SimpleNamespace(
        is_active=True,
        is_daily=False,
        min_days=1,
        max_days=10000,
        get_available_periods=list,
        can_purchase_custom_days=lambda: True,
    )
    quote = AsyncMock()
    monkeypatch.setattr(handler, 'quote_payhot_purchase', quote)
    await handler.process_payhot_period(message, user, db, state)
    quote.assert_not_called()
    state.set_state.assert_not_called()


@pytest.mark.parametrize('lang', ['ru', 'en', 'ua', 'fa', 'zh'])
def test_all_locales_correct_consent_and_methods(lang):
    t = get_texts(lang)
    for key in (
        'HOTREC_CONSENT',
        'HOTREC_CONSENT_DAILY',
        'HOTREC_METHOD',
        'HOTREC_METHOD_CARD',
        'HOTREC_METHOD_SBP',
        'HOTREC_STATE_NONE',
    ):
        value = t.t(key, 'MISSING')
        assert value != 'MISSING'
        assert 'CisPay' not in value
        assert '360' not in value


async def test_new_tariff_goes_through_explicit_method_before_quote(monkeypatch):
    callback, user, db, state = context('tariff_payhot:3')
    state.get_data.return_value = {
        'selected_tariff_id': 3,
        'selected_period': 1,
        'tariff_device_selection': {'tariff_id': 3, 'limit': 0},
    }
    monkeypatch.setattr(
        handler, 'get_payhot_recurrent_policy', AsyncMock(return_value={'enabled': True, 'methods': ['card', 'sbp']})
    )
    quote = AsyncMock()
    monkeypatch.setattr(handler, 'quote_payhot_purchase', quote)
    await handler.purchase_payhot_from_tariff(callback, user, db, state)
    quote.assert_not_called()
    selection = state.update_data.await_args.kwargs['payhot_selection']
    assert selection['device_limit'] == 0 and selection['period_days'] == 1
    rows = callback.message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
    assert len(rows) == 3


@pytest.mark.parametrize('daily,expected', [(True, [1]), (False, [1, 10000])])
async def test_tariff_menu_uses_actual_periods_not_provider_window(monkeypatch, daily, expected):
    callback, user, db, state = context('hotrec:menu:2')
    target = SimpleNamespace(id=2, tariff_id=3, status='active')
    tariff = SimpleNamespace(
        is_daily=daily, is_active=True, get_available_periods=lambda: [1, 10000], can_purchase_custom_days=lambda: False
    )
    db.scalar.return_value = target
    db.get.side_effect = [target, tariff]
    monkeypatch.setattr(handler, 'get_payhot_agreement_state', AsyncMock(return_value={'status': 'none'}))
    monkeypatch.setattr(handler, 'get_payhot_recurrent_policy', AsyncMock(return_value={'enabled': True}))
    await handler.handle_payhot_recurrent(callback, user, db, state)
    rows = callback.message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
    assert [
        int(b.callback_data.rsplit(':', 1)[-1])
        for r in rows
        for b in r
        if (b.callback_data or '').startswith('hotrec:quote:')
    ] == expected


async def test_classic_menu_uses_configured_periods(monkeypatch):
    from app.config import settings

    callback, user, db, state = context('hotrec:menu:2')
    target = SimpleNamespace(id=2, tariff_id=None, status='active')
    db.scalar.return_value = db.get.return_value = target
    monkeypatch.setattr(type(settings), 'get_available_renewal_periods', lambda self: [1, 42, 1000])
    monkeypatch.setattr(handler, 'get_payhot_agreement_state', AsyncMock(return_value={'status': 'none'}))
    monkeypatch.setattr(handler, 'get_payhot_recurrent_policy', AsyncMock(return_value={'enabled': True}))
    await handler.handle_payhot_recurrent(callback, user, db, state)
    rows = callback.message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
    assert 'hotrec:quote:2:1000' in [b.callback_data for r in rows for b in r]


async def test_owned_renewal_omits_device_override(monkeypatch):
    callback, user, db, state = context('hotrec:quote:2:30')
    db.scalar.return_value = SimpleNamespace(id=2, tariff_id=3, device_limit=7)
    monkeypatch.setattr(
        handler, 'get_payhot_recurrent_policy', AsyncMock(return_value={'enabled': True, 'methods': ['card']})
    )
    await handler.handle_payhot_recurrent(callback, user, db, state)
    assert state.update_data.await_args.kwargs['payhot_selection']['device_limit'] is None


async def test_restored_cart_wrong_token_cannot_start_method_selection(monkeypatch):
    callback, user, db, state = context('tariff_payhot:3:old')
    state.get_data.return_value = {'restored_tariff_checkout': {'cart': {'tariff_id': 3, 'period_days': 30}}}
    choose = AsyncMock()
    monkeypatch.setattr(handler, '_choose_method', choose)
    await handler.purchase_payhot_from_tariff(callback, user, db, state)
    choose.assert_not_called()


async def test_restored_purchase_requires_new_consent_and_binds_cart(monkeypatch):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from app.handlers.subscription import tariff_purchase
    from app.services import payhot_recurrent_policy

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Back', callback_data='back')]])
    user, db, state = SimpleNamespace(id=1, language='ru'), AsyncMock(), AsyncMock()
    cart = {
        'cart_mode': 'tariff_purchase',
        'tariff_id': 3,
        'period_days': 1000,
        'subscription_id': None,
        'selected_device_limit': 0,
    }
    monkeypatch.setattr(
        payhot_recurrent_policy, 'get_payhot_recurrent_policy', AsyncMock(return_value={'enabled': True})
    )
    await tariff_purchase._add_payhot_restored_purchase_button(keyboard, user, db, state, cart, cart)
    assert keyboard.inline_keyboard[0][0].callback_data.startswith('tariff_payhot:3:')
    saved = state.update_data.await_args.kwargs
    assert saved['restored_tariff_checkout']['cart']['selected_device_limit'] == 0
    assert 'payhot_consent' not in saved


@pytest.mark.parametrize(
    'enabled,custom,mode',
    [(False, None, 'tariff_purchase'), (True, 20, 'tariff_purchase'), (True, None, 'tariff_extend')],
)
async def test_restore_does_not_offer_ineligible_recurring(monkeypatch, enabled, custom, mode):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from app.handlers.subscription import tariff_purchase
    from app.services import payhot_recurrent_policy

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Back', callback_data='back')]])
    user, db, state = SimpleNamespace(id=1, language='ru'), AsyncMock(), AsyncMock()
    cart = {'cart_mode': mode, 'tariff_id': 3, 'period_days': 1, 'custom_traffic_gb': custom}
    monkeypatch.setattr(
        payhot_recurrent_policy, 'get_payhot_recurrent_policy', AsyncMock(return_value={'enabled': enabled})
    )
    await tariff_purchase._add_payhot_restored_purchase_button(keyboard, user, db, state, cart, cart)
    assert len(keyboard.inline_keyboard) == 1
    state.update_data.assert_not_called()
