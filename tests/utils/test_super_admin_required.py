"""super_admin_required: only ADMIN_IDS pass; a role-admin (even with 'settings') is denied."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery

from app.config import settings
from app.utils.decorators import super_admin_required


def _callback(user_id: int) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = SimpleNamespace(id=user_id)
    cb.answer = AsyncMock()
    return cb


async def test_superadmin_passes(monkeypatch):
    monkeypatch.setattr(type(settings), 'get_admin_ids', lambda self: [111])
    called = {'v': False}

    @super_admin_required
    async def handler(event, **kwargs):
        called['v'] = True
        return 'ok'

    result = await handler(_callback(111))
    assert result == 'ok'
    assert called['v'] is True


async def test_non_superadmin_denied(monkeypatch):
    monkeypatch.setattr(type(settings), 'get_admin_ids', lambda self: [111])
    called = {'v': False}

    @super_admin_required
    async def handler(event, **kwargs):
        called['v'] = True
        return 'ok'

    cb = _callback(222)  # a role-admin, not in ADMIN_IDS
    result = await handler(cb)
    assert result is None
    assert called['v'] is False
    cb.answer.assert_awaited()  # ACCESS_DENIED shown
