from unittest.mock import AsyncMock, MagicMock

import pytest

import app.database.crud.subscription as crud


def _db_with_active(active_gb: int):
    db = MagicMock()
    result = MagicMock()
    result.scalar.return_value = active_gb
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_cap_zero_always_allows(monkeypatch):
    monkeypatch.setattr(type(crud.settings), 'get_max_purchased_traffic_gb', lambda self: 0, raising=False)
    db = _db_with_active(999)
    allowed, remaining = await crud.can_add_purchased_traffic(db, 1, 500)
    assert allowed is True
    assert remaining == -1


@pytest.mark.asyncio
async def test_cap_allows_within(monkeypatch):
    monkeypatch.setattr(type(crud.settings), 'get_max_purchased_traffic_gb', lambda self: 100, raising=False)
    db = _db_with_active(0)
    allowed, remaining = await crud.can_add_purchased_traffic(db, 1, 50)
    assert allowed is True
    assert remaining == 100


@pytest.mark.asyncio
async def test_cap_rejects_over(monkeypatch):
    monkeypatch.setattr(type(crud.settings), 'get_max_purchased_traffic_gb', lambda self: 100, raising=False)
    db = _db_with_active(80)
    allowed, remaining = await crud.can_add_purchased_traffic(db, 1, 50)
    assert allowed is False
    assert remaining == 20


@pytest.mark.asyncio
async def test_cap_exact_boundary_allows(monkeypatch):
    monkeypatch.setattr(type(crud.settings), 'get_max_purchased_traffic_gb', lambda self: 100, raising=False)
    db = _db_with_active(80)
    allowed, remaining = await crud.can_add_purchased_traffic(db, 1, 20)
    assert allowed is True
    assert remaining == 20


@pytest.mark.asyncio
async def test_cap_full_rejects(monkeypatch):
    monkeypatch.setattr(type(crud.settings), 'get_max_purchased_traffic_gb', lambda self: 100, raising=False)
    db = _db_with_active(100)
    allowed, remaining = await crud.can_add_purchased_traffic(db, 1, 1)
    assert allowed is False
    assert remaining == 0


@pytest.mark.asyncio
async def test_get_active_sums(monkeypatch):
    db = _db_with_active(42)
    total = await crud.get_active_purchased_traffic_gb(db, 1)
    assert total == 42
