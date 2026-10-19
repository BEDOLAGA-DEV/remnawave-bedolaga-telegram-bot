"""Telegram-редактор семейства тарифа: ввод, сброс и ранняя валидация."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.handlers.admin import tariffs as mod


def _unwrap(handler):
    while hasattr(handler, '__wrapped__'):
        handler = handler.__wrapped__
    return handler


def _state():
    state = MagicMock()
    state.get_data = AsyncMock(return_value={'tariff_id': 7})
    state.set_state = AsyncMock()
    state.update_data = AsyncMock()
    state.clear = AsyncMock()
    return state


def _message(value):
    message = MagicMock()
    message.text = value
    message.answer = AsyncMock()
    return message


@pytest.mark.asyncio
async def test_family_editor_rejects_invalid_input_before_write(monkeypatch):
    tariff = SimpleNamespace(id=7, name='VPN', entitlement_family='main')
    db = MagicMock()
    update = AsyncMock()
    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(mod, 'update_tariff', update)
    state = _state()
    message = _message('Invalid Family')

    await _unwrap(mod.process_edit_tariff_entitlement_family)(message, SimpleNamespace(language='ru'), db, state)

    update.assert_not_awaited()
    state.clear.assert_not_awaited()
    message.answer.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('incoming', 'expected'),
    [('main', 'main'), ('metered_bypass', 'metered_bypass'), ('-', None)],
)
async def test_family_editor_updates_or_clears(monkeypatch, incoming, expected):
    tariff = SimpleNamespace(id=7, name='VPN', entitlement_family='main')

    async def update(_db, obj, *, entitlement_family):
        obj.entitlement_family = entitlement_family
        return obj

    editor = AsyncMock(side_effect=update)
    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(mod, 'update_tariff', editor)
    monkeypatch.setattr(mod, 'get_tariff_subscriptions_count', AsyncMock(return_value=0))
    monkeypatch.setattr(mod, 'format_tariff_info', lambda *_args: 'DETAIL')
    monkeypatch.setattr(mod, 'get_tariff_view_keyboard', lambda *_args: None)
    state = _state()
    message = _message(incoming)
    db = MagicMock()

    await _unwrap(mod.process_edit_tariff_entitlement_family)(message, SimpleNamespace(language='ru'), db, state)

    editor.assert_awaited_once_with(db, tariff, entitlement_family=expected)
    state.clear.assert_awaited_once()
    assert tariff.entitlement_family == expected


@pytest.mark.asyncio
async def test_family_editor_start_shows_current_value(monkeypatch):
    tariff = SimpleNamespace(id=7, name='Main & VPN', entitlement_family='main')
    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    callback = MagicMock()
    callback.data = 'admin_tariff_edit_family:7'
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    state = _state()

    await _unwrap(mod.start_edit_tariff_entitlement_family)(
        callback, SimpleNamespace(language='ru'), MagicMock(), state
    )

    state.set_state.assert_awaited_once_with(mod.AdminStates.editing_tariff_entitlement_family)
    state.update_data.assert_awaited_once_with(tariff_id=7)
    content = callback.message.edit_text.await_args.args[0]
    assert 'main' in content and 'Main &amp; VPN' in content
    callback.answer.assert_awaited_once()
