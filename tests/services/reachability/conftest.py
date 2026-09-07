"""Фабрика сессий на SQLite в памяти для тестов сервиса задач.

Фикстура асинхронная (pytest-asyncio), поэтому тесты, которые её берут, помечаются
``pytest.mark.asyncio`` — иначе фикстура и тест окажутся в разных циклах событий.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import (
    Base,
    ReachabilityBatch,
    ReachabilityJob,
    ReachabilityLeg,
    ReachabilityTargetPref,
    Subscription,
    User,
)
from tests.fixtures.sqlite_memory import ensure_real_aiosqlite


_TABLES = (
    User.__table__,
    Subscription.__table__,
    ReachabilityBatch.__table__,
    ReachabilityJob.__table__,
    ReachabilityLeg.__table__,
    ReachabilityTargetPref.__table__,
)


@pytest_asyncio.fixture
async def session_factory(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[async_sessionmaker]:
    ensure_real_aiosqlite(monkeypatch)
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=list(_TABLES)))
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        yield maker
    finally:
        await engine.dispose()
