"""Мультитариф: тариф уже есть — спросить «продлить имеющуюся или купить ещё одну».

Вопрос задаётся только на витрине, только в мультитарифе и только когда у
человека живая платная подписка этого тарифа. Ответ «ещё одну» ставит признак в
состоянии, и дальше покупка идёт новой подпиской.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app.database.crud.subscription as sub_crud
from app.config import Settings
from app.handlers.subscription import tariff_purchase as m
from app.handlers.subscription.common import TARIFF_COPY_STATE_KEY


def _callback(data: str = 'tariff_select:42'):
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


def _state(data: dict | None = None):
    store = dict(data or {})
    state = MagicMock()
    state.get_state = AsyncMock(return_value=None)
    state.set_state = AsyncMock()

    async def get_data():
        return dict(store)

    async def update_data(*args, **kwargs):
        for arg in args:
            store.update(arg)
        store.update(kwargs)

    state.get_data = AsyncMock(side_effect=get_data)
    state.update_data = AsyncMock(side_effect=update_data)
    state.store = store
    return state


def _user():
    user = MagicMock()
    user.id = 7
    user.language = 'ru'
    user.balance_kopeks = 100_000
    user.get_primary_promo_group = MagicMock(return_value=None)
    return user


def _tariff(tariff_id: int = 42):
    tariff = MagicMock()
    tariff.id = tariff_id
    tariff.name = 'Премиум'
    tariff.is_active = True
    tariff.is_daily = False
    tariff.can_purchase_custom_days = MagicMock(return_value=False)
    tariff.can_purchase_custom_traffic = MagicMock(return_value=False)
    tariff.period_prices = {'30': 10000}
    return tariff


def _owned(subscription_id: int, days: int):
    return SimpleNamespace(id=subscription_id, end_date=datetime.now(UTC) + timedelta(days=days), is_trial=False)


def _buttons(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


async def test_owned_tariff_asks_which_copy_to_renew_or_buy_another(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(m, 'get_tariff_by_id', AsyncMock(return_value=_tariff()))
    owned = [_owned(11, 20), _owned(12, 5)]
    lookup = AsyncMock(return_value=owned)
    monkeypatch.setattr(sub_crud, 'get_live_subscriptions_by_user_and_tariff', lookup)
    periods = MagicMock()
    monkeypatch.setattr(m, 'get_tariff_periods_keyboard', periods)

    callback = _callback()
    await m._proceed_with_selected_tariff(callback, _user(), AsyncMock(), _state(), 42)

    lookup.assert_awaited_once()
    periods.assert_not_called()
    markup = callback.message.edit_text.await_args.kwargs['reply_markup']
    assert _buttons(markup) == ['tariff_extend:11:42', 'tariff_extend:12:42', 'tariff_copy:42', 'menu_buy']


async def test_copy_answer_skips_the_question_and_opens_periods(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(m, 'get_tariff_by_id', AsyncMock(return_value=_tariff()))
    lookup = AsyncMock(return_value=[_owned(11, 20)])
    monkeypatch.setattr(sub_crud, 'get_live_subscriptions_by_user_and_tariff', lookup)
    monkeypatch.setattr(m, 'format_tariff_info_for_user', MagicMock(return_value='INFO'))
    periods_kb = MagicMock(name='periods')
    monkeypatch.setattr(m, 'get_tariff_periods_keyboard', MagicMock(return_value=periods_kb))

    callback = _callback()
    state = _state({TARIFF_COPY_STATE_KEY: 42})
    await m._proceed_with_selected_tariff(callback, _user(), AsyncMock(), state, 42)

    lookup.assert_not_awaited()
    assert callback.message.edit_text.await_args.kwargs['reply_markup'] is periods_kb


async def test_single_tariff_mode_never_asks(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(m, 'get_tariff_by_id', AsyncMock(return_value=_tariff()))
    lookup = AsyncMock(return_value=[_owned(11, 20)])
    monkeypatch.setattr(sub_crud, 'get_live_subscriptions_by_user_and_tariff', lookup)
    monkeypatch.setattr(m, 'format_tariff_info_for_user', MagicMock(return_value='INFO'))
    monkeypatch.setattr(m, 'get_tariff_periods_keyboard', MagicMock(return_value=MagicMock()))

    await m._proceed_with_selected_tariff(_callback(), _user(), AsyncMock(), _state(), 42)

    lookup.assert_not_awaited()


async def test_buy_another_sets_the_copy_flag_and_continues(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(Settings, 'get_max_active_subscriptions', lambda self: 10)
    monkeypatch.setattr(sub_crud, 'get_active_subscriptions_by_user_id', AsyncMock(return_value=[_owned(11, 20)]))
    proceed = AsyncMock()
    monkeypatch.setattr(m, '_proceed_with_selected_tariff', proceed)

    state = _state({'active_subscription_id': 11, 'target_subscription_id': 11})
    await m.buy_tariff_copy.__wrapped__(_callback('tariff_copy:42'), _user(), AsyncMock(), state)

    proceed.assert_awaited_once()
    assert proceed.await_args.args[4] == 42
    assert state.store[TARIFF_COPY_STATE_KEY] == 42
    assert state.store['active_subscription_id'] is None
    assert state.store['target_subscription_id'] is None


async def test_buy_another_respects_the_subscription_cap(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(Settings, 'get_max_active_subscriptions', lambda self: 1)
    monkeypatch.setattr(sub_crud, 'get_active_subscriptions_by_user_id', AsyncMock(return_value=[_owned(11, 20)]))
    proceed = AsyncMock()
    monkeypatch.setattr(m, '_proceed_with_selected_tariff', proceed)

    callback = _callback('tariff_copy:42')
    state = _state()
    await m.buy_tariff_copy.__wrapped__(callback, _user(), AsyncMock(), state)

    proceed.assert_not_awaited()
    assert callback.answer.await_args.kwargs.get('show_alert') is True
    assert TARIFF_COPY_STATE_KEY not in state.store


async def test_new_selection_from_the_list_clears_a_previous_copy_answer(monkeypatch):
    proceed = AsyncMock()
    monkeypatch.setattr(m, '_proceed_with_selected_tariff', proceed)

    state = _state({TARIFF_COPY_STATE_KEY: 42})
    await m.select_tariff.__wrapped__(_callback('tariff_select:42'), _user(), AsyncMock(), state)

    assert state.store[TARIFF_COPY_STATE_KEY] is None
    proceed.assert_awaited_once()


async def test_copy_purchase_has_no_subscription_to_extend(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    lookup = AsyncMock()
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', lookup)
    monkeypatch.setattr(m, 'get_active_subscriptions_by_user_id', AsyncMock(return_value=[_owned(11, 20)]))

    result = await m._resolve_purchase_subscription(
        AsyncMock(), _user(), _tariff(), source_data={TARIFF_COPY_STATE_KEY: 42, 'active_subscription_id': 11}
    )

    assert result is None
    lookup.assert_not_awaited()
