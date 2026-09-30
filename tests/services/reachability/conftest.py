"""Фабрика сессий на SQLite для тестов сервиса задач.

База — файл во временной папке, а не ``:memory:``: у in-memory движка один
StaticPool-коннект на все сессии, и параллельные задачи пачки делили одну
транзакцию — rollback закрывшейся сессии стирал коммиты соседней. В проде у
каждой сессии своё соединение; файл даёт тестам ту же изоляцию.

Фикстура асинхронная (pytest-asyncio), поэтому тесты, которые её берут, помечаются
``pytest.mark.asyncio`` — иначе фикстура и тест окажутся в разных циклах событий.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

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
async def session_factory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[async_sessionmaker]:
    ensure_real_aiosqlite(monkeypatch)
    # timeout: параллельные сессии ждут блокировку записи, а не падают с «database is locked»
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path}/reachability.db', connect_args={'timeout': 30})
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=list(_TABLES)))
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        yield maker
    finally:
        await engine.dispose()
