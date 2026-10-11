"""Issue #3012 (switch flows): tariff-switch confirm callbacks end in
tariff_id/period, not a subscription_id. The switch resolver must take the
subscription from FSM active_subscription_id (set by the switch entry), NEVER
from the trailing callback segment — otherwise, when that trailing number equals
one of the user's subscription ids, the WRONG subscription is switched/charged.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import app.database.crud.subscription as subcrud
import app.database.crud.user as user_crud
import app.handlers.subscription.tariff_purchase as tp
from app.config import Settings
from app.handlers.subscription.tariff_purchase import (
    _clear_state_keeping_switch_subscription,
    _resolve_switch_subscription,
    _target_tariff_owned_elsewhere,
)


class _FakeState:
    """FSM в памяти: ``clear()`` стирает всё, как настоящий."""

    def __init__(self, data: dict):
        self.data = dict(data)

    async def get_data(self) -> dict:
        return dict(self.data)

    async def clear(self) -> None:
        self.data = {}

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)


def _patch_get_sub_by_id(monkeypatch):
    """get_subscription_by_id_for_user(db, sub_id, user_id) -> a sub with that id."""

    async def fake_get(db, sub_id, user_id):
        sub = MagicMock()
        sub.id = sub_id
        return sub

    monkeypatch.setattr(subcrud, 'get_subscription_by_id_for_user', fake_get)


async def test_switch_resolver_prefers_fsm_over_callback_trailing(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    _patch_get_sub_by_id(monkeypatch)

    state = AsyncMock()
    state.get_data = AsyncMock(return_value={'active_subscription_id': 22})

    callback = MagicMock()
    # trailing 30 = PERIOD that also equals another subscription's id — the bug case
    callback.data = 'tariff_sw_confirm:2:30'
    db_user = MagicMock()
    db_user.id = 1

    sub, sub_id = await _resolve_switch_subscription(callback, db_user, AsyncMock(), state)

    assert sub_id == 22  # from FSM, NOT 30 (the callback trailing)
    assert sub.id == 22


async def test_switch_resolver_falls_back_to_single_active_when_no_fsm(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)

    only_sub = MagicMock()
    only_sub.id = 7

    async def fake_active(db, user_id):
        return [only_sub]

    monkeypatch.setattr(subcrud, 'get_active_subscriptions_by_user_id', fake_active)

    state = AsyncMock()
    state.get_data = AsyncMock(return_value={})  # no active_subscription_id

    callback = MagicMock()
    callback.data = 'instant_sw_confirm:30'  # trailing 30 must NOT be used as a sub id
    db_user = MagicMock()
    db_user.id = 1

    sub, sub_id = await _resolve_switch_subscription(callback, db_user, AsyncMock(), state)
    assert sub_id == 7  # single active, never 30


async def test_switch_resolver_asks_to_choose_when_ambiguous(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)

    async def fake_active(db, user_id):
        return [MagicMock(id=11), MagicMock(id=12)]

    monkeypatch.setattr(subcrud, 'get_active_subscriptions_by_user_id', fake_active)

    state = AsyncMock()
    state.get_data = AsyncMock(return_value={})

    callback = MagicMock()
    callback.data = 'daily_tariff_switch_confirm:30'
    callback.answer = AsyncMock()
    db_user = MagicMock()
    db_user.id = 1

    sub, sub_id = await _resolve_switch_subscription(callback, db_user, AsyncMock(), state)
    # Multiple subs, no FSM → refuse rather than guess the trailing number
    assert sub is None
    assert sub_id is None
    callback.answer.assert_awaited()


async def test_switch_entry_keeps_pinned_subscription_through_state_clear():
    """Вход в смену чистит FSM, но выбранную в карточке подписку оставляет."""
    state = _FakeState({'active_subscription_id': 22, 'stale_key': 'x'})

    await _clear_state_keeping_switch_subscription(state)

    assert state.data == {'active_subscription_id': 22}


async def test_switch_entry_clears_state_without_pin():
    state = _FakeState({'stale_key': 'x'})

    await _clear_state_keeping_switch_subscription(state)

    assert state.data == {}


async def test_instant_switch_list_resolves_pinned_subscription_among_several(monkeypatch):
    """Две живые подписки, в FSM — выбранная в карточке: список смены берёт её.

    Раньше вход сначала чистил FSM и только потом искал подписку — при двух
    подписках смена упиралась в «Выберите подписку»."""
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    _patch_get_sub_by_id(monkeypatch)
    monkeypatch.setattr(
        subcrud,
        'get_active_subscriptions_by_user_id',
        AsyncMock(return_value=[MagicMock(id=11), MagicMock(id=22)]),
    )
    # Дальше резолва не идём: «тариф не найден» — уже после выбора подписки.
    tariff_lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(tp, 'get_tariff_by_id', tariff_lookup)

    state = _FakeState({'active_subscription_id': 22})
    callback = MagicMock()
    callback.data = 'instant_switch'
    callback.answer = AsyncMock()
    db_user = MagicMock(id=1, language='ru')

    await tp.show_instant_switch_list(callback, db_user, AsyncMock(), state)

    tariff_lookup.assert_awaited_once()
    assert all('Выберите подписку' not in str(call) for call in callback.answer.await_args_list)


async def test_target_tariff_owned_by_another_subscription(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(subcrud, 'get_subscription_by_user_and_tariff', AsyncMock(return_value=MagicMock(id=2)))

    assert await _target_tariff_owned_elsewhere(AsyncMock(), 1, 5, 1) is True
    # Та же подписка уже на этом тарифе — это не «чужая», решает «Уже на этом тарифе».
    assert await _target_tariff_owned_elsewhere(AsyncMock(), 1, 5, 2) is False


async def test_target_tariff_not_owned(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(subcrud, 'get_subscription_by_user_and_tariff', AsyncMock(return_value=None))

    assert await _target_tariff_owned_elsewhere(AsyncMock(), 1, 5, 1) is False


async def test_target_tariff_guard_is_off_in_single_mode(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    lookup = AsyncMock(return_value=MagicMock(id=2))
    monkeypatch.setattr(subcrud, 'get_subscription_by_user_and_tariff', lookup)

    assert await _target_tariff_owned_elsewhere(AsyncMock(), 1, 5, 1) is False
    lookup.assert_not_awaited()


def _confirm_callback(data: str) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def test_instant_switch_confirm_refuses_owned_tariff_before_charging(monkeypatch):
    """Кнопка из старого сообщения: целевой тариф уже куплен отдельной подпиской.

    Подтверждение отказывает до списания, а не падает на уникальном индексе после."""
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(tp, 'get_tariff_by_id', AsyncMock(return_value=MagicMock(id=5, is_active=True)))
    monkeypatch.setattr(tp, '_resolve_switch_subscription', AsyncMock(return_value=(MagicMock(id=1), 1)))
    monkeypatch.setattr(subcrud, 'get_subscription_by_user_and_tariff', AsyncMock(return_value=MagicMock(id=2)))
    charge = AsyncMock()
    monkeypatch.setattr(tp, 'subtract_user_balance', charge)
    callback = _confirm_callback('instant_sw_confirm:5')

    await tp.confirm_instant_switch(callback, MagicMock(id=1, language='ru'), AsyncMock(), _FakeState({}))

    charge.assert_not_awaited()
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get('show_alert') is True


async def test_period_switch_confirm_refuses_owned_tariff_before_charging(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    tariff = MagicMock(id=5, is_active=True, period_prices={'30': 10000})
    monkeypatch.setattr(tp, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    db_user = MagicMock(id=1, language='ru')
    monkeypatch.setattr(user_crud, 'lock_user_for_pricing', AsyncMock(return_value=db_user))
    monkeypatch.setattr(tp, '_resolve_switch_subscription', AsyncMock(return_value=(MagicMock(id=1), 1)))
    monkeypatch.setattr(subcrud, 'get_subscription_by_user_and_tariff', AsyncMock(return_value=MagicMock(id=2)))
    charge = AsyncMock()
    monkeypatch.setattr(tp, 'subtract_user_balance', charge)
    callback = _confirm_callback('tariff_sw_confirm:5:30')

    await tp.confirm_tariff_switch(callback, db_user, AsyncMock(), _FakeState({}))

    charge.assert_not_awaited()
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get('show_alert') is True
