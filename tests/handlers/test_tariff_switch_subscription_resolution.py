"""Issue #3012 (switch flows): tariff-switch confirm callbacks end in
tariff_id/period, not a subscription_id. The switch resolver must take the
subscription from FSM active_subscription_id (set by the switch entry), NEVER
from the trailing callback segment — otherwise, when that trailing number equals
one of the user's subscription ids, the WRONG subscription is switched/charged.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import app.database.crud.subscription as subcrud
from app.config import Settings
from app.handlers.subscription.tariff_purchase import _resolve_switch_subscription


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
