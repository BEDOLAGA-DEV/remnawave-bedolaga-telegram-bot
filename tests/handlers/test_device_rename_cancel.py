"""Regression: cancelling a device rename must return the user to the device list.

Telegram bug report (topic «Баги», msg 613574): "в момент переименовки устройства
нажимаешь отменить — ничего не выводится, не переходит на список устройств, нет
кнопки назад". The rename prompt was edited in with no keyboard at all, and the
typed `/cancel` path only printed "Переименование отменено" + cleared the FSM
state — a dead-end with no list and no back button.

This test drives the real handlers (with the heavy I/O mocked) to prove that:
- the «Отмена» button (`cancel_device_rename`) clears state AND re-renders the
  device list at the page the user came from;
- a typed `/cancel` now also re-renders the list instead of dead-ending;
- a valid name saves + re-renders;
- an empty-after-normalize input keeps the FSM state so the user can retry.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.handlers.subscription.devices as devices_mod


def _make_callback():
    cb = MagicMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _make_user():
    user = MagicMock()
    user.id = 1
    user.language = 'ru'
    user.remnawave_uuid = 'uuid-x'
    return user


def _service_returning(devices):
    api = AsyncMock()
    api._make_request = AsyncMock(return_value={'response': {'devices': devices}})

    @asynccontextmanager
    async def _cm():
        yield api

    svc = MagicMock()
    svc.get_api_client = MagicMock(side_effect=lambda: _cm())
    return svc


def _db_with_subscription():
    db = MagicMock()
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=MagicMock())
    db.execute = AsyncMock(return_value=res)
    return db


@pytest.mark.anyio('asyncio')
async def test_cancel_button_reopens_device_list():
    cb, user, db = _make_callback(), _make_user(), _db_with_subscription()
    state = MagicMock()
    state.get_data = AsyncMock(return_value={'rename_page': 3, 'rename_sub_id': 7})
    state.clear = AsyncMock()

    with (
        patch.object(devices_mod, 'show_devices_page', new=AsyncMock()) as show,
        patch.object(devices_mod, 'RemnaWaveService', return_value=_service_returning([{'hwid': 'AA'}])),
        patch.object(devices_mod, '_get_remnawave_uuid', return_value='uuid-x'),
    ):
        await devices_mod.cancel_device_rename(cb, user, db, state)

    state.clear.assert_awaited_once()
    show.assert_awaited_once()
    _, kwargs = show.call_args
    assert kwargs['page'] == 3 and kwargs['sub_id'] == 7
    assert show.call_args.args[2] == [{'hwid': 'AA'}]


async def _run_process(raw, *, normalize_to=None):
    message = MagicMock()
    message.text = raw
    message.answer = AsyncMock()
    user, db = _make_user(), _db_with_subscription()
    state = MagicMock()
    state.get_data = AsyncMock(return_value={'rename_hwid': 'HW', 'rename_page': 2, 'rename_sub_id': 5})
    state.clear = AsyncMock()

    patches = [
        # the re-render block does a *local* import — patch the source module.
        patch('app.services.remnawave_service.RemnaWaveService', return_value=_service_returning([{'hwid': 'HW'}])),
        patch.object(devices_mod, '_enrich_devices_with_aliases', new=AsyncMock(side_effect=lambda lst, uid: lst)),
        patch.object(devices_mod, 'get_devices_management_keyboard', return_value='KB'),
        patch.object(devices_mod, '_get_remnawave_uuid', return_value='uuid-x'),
        patch.object(devices_mod, 'upsert_alias', new=AsyncMock(return_value=(raw or '').strip())),
        patch.object(devices_mod, 'delete_alias', new=AsyncMock()),
    ]
    if normalize_to is not None:
        patches.append(patch.object(devices_mod, 'normalize_alias', return_value=normalize_to))

    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await devices_mod.process_device_rename(message, user, db, state)

    rerendered = any(c.kwargs.get('reply_markup') == 'KB' for c in message.answer.await_args_list)
    return state, message, rerendered


@pytest.mark.anyio('asyncio')
async def test_typed_cancel_reopens_device_list():
    state, message, rerendered = await _run_process('/cancel')
    state.clear.assert_awaited_once()
    assert rerendered, 'typed /cancel must re-render the device list, not dead-end'


@pytest.mark.anyio('asyncio')
async def test_valid_name_saves_and_reopens():
    state, message, rerendered = await _run_process('My Phone')
    state.clear.assert_awaited_once()
    assert rerendered


@pytest.mark.anyio('asyncio')
async def test_empty_after_normalize_keeps_state_for_retry():
    # non-empty raw that normalises to '' must NOT clear state (user retries).
    state, message, rerendered = await _run_process('zzz', normalize_to='')
    state.clear.assert_not_awaited()
    assert not rerendered
