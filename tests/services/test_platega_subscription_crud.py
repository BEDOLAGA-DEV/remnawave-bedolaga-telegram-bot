"""Тесты модели и CRUD PlategaSubscription: колонки таблицы + round-trip через реальную сессию."""

import contextlib
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.crud import platega_subscription as crud
from app.database.models import Base, PlategaSubscription


def test_model_table_and_columns():
    assert PlategaSubscription.__tablename__ == 'platega_subscriptions'
    cols = set(PlategaSubscription.__table__.columns.keys())
    assert {'platega_subscription_id', 'interval', 'charge_days', 'amount_kopeks', 'status'} <= cols


def _ensure_real_aiosqlite(monkeypatch) -> None:
    """Снять фиктивный sys.modules['aiosqlite'] перед созданием реального engine.

    ``tests/conftest.py`` подставляет пустой ``ModuleType('aiosqlite')`` через
    ``sys.modules.setdefault(...)`` для окружений без реального пакета. Но эта
    строчка выполняется раньше, чем что-либо успевает импортировать настоящий
    aiosqlite, поэтому заглушка перекрывает и физически установленный пакет —
    ``create_async_engine('sqlite+aiosqlite:///:memory:')`` падает с
    ``AttributeError: module 'aiosqlite' has no attribute 'Connection'``.
    Удаляем нерабочую заглушку (нет ``connect``): следующий ``import aiosqlite``
    внутри диалекта SQLAlchemy подтянет настоящий установленный модуль.
    """
    stub = sys.modules.get('aiosqlite')
    if stub is not None and not hasattr(stub, 'connect'):
        monkeypatch.delitem(sys.modules, 'aiosqlite', raising=False)


@contextlib.asynccontextmanager
async def _memory_session(monkeypatch):
    """Реальная in-memory SQLite сессия только с таблицей platega_subscriptions.

    Полный create_all не годится (другие таблицы используют JSONB, SQLite не
    компилирует), а FK в SQLite по умолчанию не форсятся — поэтому user_id/
    subscription_id можно ставить произвольными (1), реальные строки не нужны.
    """
    _ensure_real_aiosqlite(monkeypatch)
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[PlategaSubscription.__table__]))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()


async def test_create_and_fetch_round_trip(monkeypatch):
    """create → get_by_platega_id / get_active_by_subscription → update → CANCELLED больше не активна."""
    async with _memory_session(monkeypatch) as db:
        created = await crud.create_platega_subscription(
            db,
            user_id=1,
            subscription_id=1,
            tariff_id=None,
            interval=3,
            charge_days=30,
            amount_kopeks=19900,
            redirect_url='https://pay.platega.io/s/1',
            platega_subscription_id='sub-1',
        )
        assert created.status == 'PENDING'

        by_platega = await crud.get_platega_subscription_by_platega_id(db, 'sub-1')
        assert by_platega is not None
        assert by_platega.id == created.id

        active = await crud.get_active_platega_subscription_by_subscription(db, 1)
        assert active is not None
        assert active.id == created.id

        updated = await crud.update_platega_subscription(db, created, status='CANCELLED')
        assert updated.status == 'CANCELLED'

        assert await crud.get_active_platega_subscription_by_subscription(db, 1) is None
