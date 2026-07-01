"""bot_role_save/toggle must not silently wipe permissions on lost FSM state."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery

from app.config import settings
import app.handlers.admin.bot_roles as br


@pytest.fixture
def as_superadmin(monkeypatch):
    # `settings` is a pydantic BaseSettings instance; only the class allows
    # attribute patching for methods, so patch the class, not the instance.
    monkeypatch.setattr(type(settings), 'get_admin_ids', lambda self: [111])
    return 111


def _callback(admin_id: int, data: str) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = SimpleNamespace(id=admin_id)
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    return cb


def _state(data: dict) -> MagicMock:
    state = MagicMock()
    state.get_data = AsyncMock(return_value=data)
    state.clear = AsyncMock()
    return state


def _db_user():
    return SimpleNamespace(id=1, telegram_id=111, language='ru')


async def test_save_with_lost_state_does_not_wipe(as_superadmin):
    cb = _callback(as_superadmin, 'bot_role_save:5')
    state = _state({})  # state lost -> no 'selected_permissions' key
    db = MagicMock()
    db.commit = AsyncMock()

    with patch.object(br.BotRoleCRUD, 'set_bot_role', new=AsyncMock()) as set_role:
        await br.bot_role_save(cb, db_user=_db_user(), state=state, db=db)

    set_role.assert_not_awaited()
    db.commit.assert_not_awaited()
    cb.answer.assert_awaited()  # user told the session expired


async def test_save_with_empty_selection_rejected(as_superadmin):
    cb = _callback(as_superadmin, 'bot_role_save:5')
    state = _state({'selected_permissions': []})  # explicitly empty
    db = MagicMock()
    db.commit = AsyncMock()

    with patch.object(br.BotRoleCRUD, 'set_bot_role', new=AsyncMock()) as set_role:
        await br.bot_role_save(cb, db_user=_db_user(), state=state, db=db)

    set_role.assert_not_awaited()


async def test_save_happy_path_persists(as_superadmin):
    cb = _callback(as_superadmin, 'bot_role_save:5')
    state = _state({'selected_permissions': ['support']})
    db = MagicMock()
    db.commit = AsyncMock()

    with patch.object(br.BotRoleCRUD, 'set_bot_role', new=AsyncMock()) as set_role, \
         patch.object(br.BotRoleCRUD, 'list_bot_roles', new=AsyncMock(return_value=[])):
        await br.bot_role_save(cb, db_user=_db_user(), state=state, db=db)

    set_role.assert_awaited_once()
    assert set_role.await_args.args[2] == ['support']
    db.commit.assert_awaited_once()
