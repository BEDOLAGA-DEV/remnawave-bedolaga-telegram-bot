"""Tests for the registration-completion drain of ``pending_subid``.

The parser half (``_split_start_param_subid``) is covered by 12 unit tests in
``tests/handlers/test_start_subid.py``. This file pins the OTHER half — the
``_persist_pending_subid_after_registration`` drain that runs from all three
registration-completion paths (cmd_start fast-path,
complete_registration_from_callback, complete_registration).

Without these tests, a typo in the FSM key name (`pending_sub_id` vs
`pending_subid`), a wrong argument order to ``upsert_subid``, or a future
rename of the FSM key would silently drop every Keitaro click ID without
breaking any existing test.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.handlers import start as start_module
from app.handlers.start import _persist_pending_subid_after_registration


@pytest.mark.anyio('asyncio')
async def test_drain_calls_upsert_subid_with_pending_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: FSM has pending_subid → drain calls upsert_subid with
    (db, user.id, subid, source='telegram'). Pins the exact call shape."""
    user = SimpleNamespace(id=42)
    state = SimpleNamespace(get_data=AsyncMock(return_value={'pending_subid': 'KEITARO_CLICK_X'}))

    upsert_mock = AsyncMock()
    # The drain imports upsert_subid lazily inside the function — patch the source module.
    monkeypatch.setattr('app.database.crud.yandex_client_id.upsert_subid', upsert_mock)

    db = SimpleNamespace()
    await _persist_pending_subid_after_registration(db, state, user)

    upsert_mock.assert_awaited_once_with(db, 42, 'KEITARO_CLICK_X', source='telegram')


@pytest.mark.anyio('asyncio')
async def test_drain_is_noop_when_no_pending_subid(monkeypatch: pytest.MonkeyPatch) -> None:
    """No pending_subid in FSM → drain returns without calling upsert. This is the
    common case for users who came to /start without an affiliate link."""
    user = SimpleNamespace(id=42)
    state = SimpleNamespace(get_data=AsyncMock(return_value={}))

    upsert_mock = AsyncMock()
    monkeypatch.setattr('app.database.crud.yandex_client_id.upsert_subid', upsert_mock)

    await _persist_pending_subid_after_registration(SimpleNamespace(), state, user)

    upsert_mock.assert_not_called()


@pytest.mark.anyio('asyncio')
async def test_drain_is_noop_when_pending_subid_is_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty-string subid (defensive — parser rejects this, but a future bug or
    DB-side coercion could leave one) → drain treats as missing, no DB write."""
    user = SimpleNamespace(id=42)
    state = SimpleNamespace(get_data=AsyncMock(return_value={'pending_subid': ''}))

    upsert_mock = AsyncMock()
    monkeypatch.setattr('app.database.crud.yandex_client_id.upsert_subid', upsert_mock)

    await _persist_pending_subid_after_registration(SimpleNamespace(), state, user)

    upsert_mock.assert_not_called()


@pytest.mark.anyio('asyncio')
async def test_drain_swallows_upsert_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """When upsert_subid raises (DB transient, FK violation), the drain must catch
    and log — otherwise a Keitaro DB issue would crash every new user's registration
    completion path. Caller (cmd_start / complete_registration_*) explicitly clears
    pending_subid afterwards, so the in-FSM state isn't replayed."""
    user = SimpleNamespace(id=42)
    state = SimpleNamespace(get_data=AsyncMock(return_value={'pending_subid': 'X'}))

    failing_upsert = AsyncMock(side_effect=RuntimeError('db down'))
    monkeypatch.setattr('app.database.crud.yandex_client_id.upsert_subid', failing_upsert)

    # Must NOT raise — exception is caught and logged.
    await _persist_pending_subid_after_registration(SimpleNamespace(), state, user)

    failing_upsert.assert_awaited_once()


@pytest.mark.anyio('asyncio')
async def test_drain_is_noop_when_get_data_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """state.get_data() returning None (aiogram quirk on fresh state) → drain treats
    as no pending data. The `data = ... or {}` guard in the function handles this."""
    user = SimpleNamespace(id=42)
    state = SimpleNamespace(get_data=AsyncMock(return_value=None))

    upsert_mock = AsyncMock()
    monkeypatch.setattr('app.database.crud.yandex_client_id.upsert_subid', upsert_mock)

    await _persist_pending_subid_after_registration(SimpleNamespace(), state, user)

    upsert_mock.assert_not_called()


def test_drain_function_imported_from_start_module() -> None:
    """The drain function must remain importable as a public(ish) symbol on
    `app.handlers.start` — three completion-path call sites rely on this binding.
    A rename or accidental removal would surface here before in production."""
    assert callable(start_module._persist_pending_subid_after_registration)
