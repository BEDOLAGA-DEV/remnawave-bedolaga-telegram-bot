import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import app.database.crud.server_squad as ss


@pytest.mark.asyncio
async def test_set_default_clears_others_and_sets_target(monkeypatch):
    server = SimpleNamespace(id=7, squad_uuid='u7', is_default=False)

    async def fake_get(db, server_id):
        return server

    monkeypatch.setattr(ss, 'get_server_squad_by_id', fake_get)

    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

    result = await ss.set_default_server_squad(db, 7)

    assert db.execute.await_count == 2
    db.commit.assert_awaited_once()
    assert result is server


@pytest.mark.asyncio
async def test_set_default_returns_none_when_missing(monkeypatch):
    async def fake_get(db, server_id):
        return None

    monkeypatch.setattr(ss, 'get_server_squad_by_id', fake_get)

    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

    result = await ss.set_default_server_squad(db, 999)

    assert result is None
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()
