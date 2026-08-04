"""In-memory SQLite сессия для тестов, которым нужны реальные запросы к БД.

Полный ``create_all`` не годится: часть таблиц использует JSONB, поэтому здесь
регистрируется рендер JSONB как JSON и таблицы создаются точечным списком.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import AsyncIterator, Sequence

from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.database.models import Base


@compiles(JSONB, 'sqlite')
def _compile_jsonb_on_sqlite(type_, compiler, **kw) -> str:
    """SQLite не компилирует JSONB (например, User.notification_settings)."""
    return 'JSON'


def ensure_real_aiosqlite(monkeypatch) -> None:
    """Снять заглушку sys.modules['aiosqlite'] из conftest перед созданием engine.

    conftest подставляет пустой модуль для окружений без aiosqlite, и эта заглушка
    перекрывает физически установленный пакет — create_async_engine падает.
    """
    stub = sys.modules.get('aiosqlite')
    if stub is not None and not hasattr(stub, 'connect'):
        monkeypatch.delitem(sys.modules, 'aiosqlite', raising=False)


@contextlib.asynccontextmanager
async def memory_session(monkeypatch, tables: Sequence[Table]) -> AsyncIterator[AsyncSession]:
    """Сессия к :memory: БД, где созданы только переданные таблицы."""
    ensure_real_aiosqlite(monkeypatch)
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=list(tables)))
    # autoflush=False повторяет прод (app/database/database.py): иначе тест
    # видит ещё не отправленные в БД изменения, которых в проде на этом месте
    # не будет, и пропускает целый класс ошибок «SELECT читает старое значение».
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()
