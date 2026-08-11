"""Guard for the issue #3012 footgun in resolve_subscription_from_context.

The whole codebase relies on one safety property: the resolver only treats a
callback's TRAILING segment as a subscription_id when the callback is
COLON-separated (`prefix:sub_id`). Underscore-suffixed callbacks (`add_traffic_30`,
`change_devices_5`, `autopay_days_7`) must NOT have their trailing number used as a
subscription_id — they fall through to FSM `active_subscription_id`. If a future
change to the resolver breaks this (e.g. also splitting on `_`), every
underscore-callback handler would silently regress into the wrong-subscription bug.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import app.database.crud.subscription as subcrud
from app.config import Settings
from app.handlers.subscription.common import resolve_subscription_from_context


def _patch_get_sub_by_id(monkeypatch):
    async def fake_get(db, sub_id, user_id):
        sub = MagicMock()
        sub.id = sub_id
        return sub

    monkeypatch.setattr(subcrud, 'get_subscription_by_id_for_user', fake_get)


async def test_underscore_trailing_number_is_NOT_used_as_sub_id(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    _patch_get_sub_by_id(monkeypatch)

    state = AsyncMock()
    state.get_data = AsyncMock(return_value={'active_subscription_id': 22})

    callback = MagicMock()
    # underscore-suffixed: the trailing 30 must be ignored by priority #1
    callback.data = 'add_traffic_30'
    db_user = MagicMock()
    db_user.id = 1

    sub, sub_id = await resolve_subscription_from_context(callback, db_user, AsyncMock(), state)
    assert sub_id == 22  # from FSM, NOT 30 (underscore trailing is not a colon sub_id)


async def test_colon_trailing_real_sub_id_is_used(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    _patch_get_sub_by_id(monkeypatch)

    state = AsyncMock()
    state.get_data = AsyncMock(return_value={'active_subscription_id': 99})

    callback = MagicMock()
    # legit colon callback carrying the real subscription id last
    callback.data = 'subscription_connect:22'
    db_user = MagicMock()
    db_user.id = 1

    sub, sub_id = await resolve_subscription_from_context(callback, db_user, AsyncMock(), state)
    assert sub_id == 22  # colon trailing IS the sub_id (legit priority #1)
