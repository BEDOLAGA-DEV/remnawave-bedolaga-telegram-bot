"""Сессии к настоящему PostgreSQL для тестов, которым SQLite не годится.

SQLite молча игнорирует ``FOR UPDATE``, не проверяет длину ``VARCHAR(n)`` и
иначе хранит время и JSON. Всё, что зависит от этих свойств — блокировки строк,
конкурентная обработка уведомлений, ограничения схемы — на SQLite проверяется
только для вида. Прод работает на PostgreSQL, поэтому такие проверки должны
идти на нём.

Схема создаётся ``Base.metadata.create_all`` — ровно так, как её получает
свежая боевая база: ``app/database/migrations.py`` на пустой БД делает
``create_all`` и ``alembic stamp head``, а не прогон цепочки миграций. Прогнать
цепочку с нуля и нельзя: ревизия ``0001`` сама вызывает ``create_all``, после
чего ревизия ``0021`` падает на ``operator does not exist: json <> unknown`` —
данные уже в новом формате. То есть ``create_all`` здесь не упрощение, а
воспроизведение боевого пути.

База берётся из ``TEST_DATABASE_URL`` (asyncpg-URL). Если переменной нет,
тесты пропускаются — окружение без PostgreSQL не должно ронять прогон. В CI
это опасно: пропуск выглядит как успех. Поэтому там выставляется
``REQUIRE_POSTGRES_TESTS=1``, и отсутствие базы становится падением.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections.abc import AsyncIterator, Iterator, Sequence

import pytest
from sqlalchemy import Table, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.database.models import Base


TEST_DATABASE_URL_ENV = 'TEST_DATABASE_URL'
REQUIRE_POSTGRES_ENV = 'REQUIRE_POSTGRES_TESTS'

_TRUE_VALUES = frozenset({'1', 'true', 'yes', 'y', 'on'})

# Пересоздание схемы не должно ждать чужую транзакцию дольше нескольких
# секунд: лучше внятная ошибка, чем бесконечное молчание.
SCHEMA_LOCK_TIMEOUT_MS = 5000


def postgres_dsn() -> str | None:
    """URL тестовой базы из окружения или ``None``."""
    return os.environ.get(TEST_DATABASE_URL_ENV, '').strip() or None


def postgres_is_required() -> bool:
    """Требует ли окружение, чтобы тесты на PostgreSQL действительно шли."""
    return os.environ.get(REQUIRE_POSTGRES_ENV, '').strip().lower() in _TRUE_VALUES


def require_postgres_dsn() -> str:
    """URL живого PostgreSQL, иначе пропуск теста (или падение, если требуется)."""
    dsn = postgres_dsn()
    if dsn:
        return dsn

    reason = f'{TEST_DATABASE_URL_ENV} не задан — тесты на настоящем PostgreSQL пропущены'
    if postgres_is_required():
        pytest.fail(f'{reason}, но {REQUIRE_POSTGRES_ENV} требует их запуска')
    pytest.skip(reason)
    raise AssertionError('недостижимо')  # pragma: no cover - pytest.skip бросает исключение


@contextlib.contextmanager
def real_asyncpg() -> Iterator[None]:
    """Снимает заглушку ``sys.modules['asyncpg']``, поставленную conftest.

    conftest подставляет пустой модуль для окружений без драйвера, и он
    перекрывает физически установленный пакет — ``create_async_engine`` падает.
    Обратно заглушка возвращается только если настоящий драйвер так и не
    загрузился: подменять уже импортированный диалектом модуль нельзя.
    """
    stub = sys.modules.get('asyncpg')
    if stub is None or hasattr(stub, 'connect'):
        yield
        return

    del sys.modules['asyncpg']
    try:
        yield
    finally:
        sys.modules.setdefault('asyncpg', stub)


async def _recreate_schema(dsn: str) -> None:
    """Сносит содержимое базы и создаёт схему проекта заново.

    ``DROP SCHEMA`` берёт ACCESS EXCLUSIVE и потому ждёт КАЖДОЕ открытое
    соединение к базе. Прерванный прогон (Ctrl-C, таймаут) оставляет за собой
    транзакцию — и следующий запуск повисает молча, без единой строки в выводе.
    Худший из возможных исходов: выглядит как «тесты идут», а на деле не
    начались. Поэтому: сначала выгоняем чужие соединения, потом ограничиваем
    ожидание. База тут выделенная под тесты — отключать в ней некого, кроме
    таких же брошенных прогонов.
    """
    engine = create_async_engine(dsn, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    'SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                    'WHERE datname = current_database() AND pid <> pg_backend_pid()'
                )
            )
        async with engine.begin() as conn:
            await conn.execute(text(f"SET LOCAL lock_timeout = '{SCHEMA_LOCK_TIMEOUT_MS}ms'"))
            await conn.execute(text('DROP SCHEMA IF EXISTS public CASCADE'))
            await conn.execute(text('CREATE SCHEMA public'))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except DBAPIError as error:
        raise RuntimeError(
            f'не удалось пересоздать схему в {TEST_DATABASE_URL_ENV}: {error}. '
            'Обычно это чужое открытое соединение к тестовой базе — проверьте '
            'pg_stat_activity или пересоздайте контейнер (make pg-test-up).'
        ) from error
    finally:
        await engine.dispose()


@pytest.fixture(scope='session')
def postgres_database() -> str:
    """URL тестовой базы, в которой уже создана полная схема проекта.

    Фикстура синхронная намеренно: conftest создаёт отдельный цикл событий на
    каждый тест, поэтому движок нельзя переносить между тестами. Схема строится
    в собственном одноразовом цикле, движок тут же закрывается.

    Схему достаточно создать один раз за прогон — это обеспечивает область
    ``session``; дальше тесты чистят свои таблицы через TRUNCATE (9 мс против
    0.8 с на пересоздание всех 118 таблиц).
    """
    dsn = require_postgres_dsn()

    with real_asyncpg():
        asyncio.run(_recreate_schema(dsn))
    return dsn


async def truncate_tables(engine: AsyncEngine, tables: Sequence[Table]) -> None:
    """Очищает переданные таблицы вместе со счётчиками идентификаторов."""
    if not tables:
        return
    targets = ', '.join(f'"{table.name}"' for table in tables)
    async with engine.begin() as conn:
        await conn.execute(text(f'TRUNCATE {targets} RESTART IDENTITY CASCADE'))


@contextlib.asynccontextmanager
async def postgres_engine(dsn: str, tables: Sequence[Table] = ()) -> AsyncIterator[AsyncEngine]:
    """Движок к тестовой базе; переданные таблицы очищаются до и после теста.

    ``NullPool`` здесь обязателен: каждая сессия получает собственное
    соединение, иначе тесты на блокировки проверяли бы блокировку сессии самой
    себя — а такой запрос не ждёт и проходит насквозь.
    """
    with real_asyncpg():
        engine = create_async_engine(dsn, poolclass=NullPool)
    try:
        await truncate_tables(engine, tables)
        yield engine
    finally:
        with contextlib.suppress(Exception):
            await truncate_tables(engine, tables)
        await engine.dispose()


@contextlib.asynccontextmanager
async def postgres_session(dsn: str, tables: Sequence[Table] = ()) -> AsyncIterator[AsyncSession]:
    """Одна сессия к тестовой базе (зеркало ``memory_session``, но на PostgreSQL)."""
    async with postgres_engine(dsn, tables) as engine:
        # autoflush=False повторяет прод (app/database/database.py).
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as session:
            yield session


@contextlib.asynccontextmanager
async def postgres_sessions(
    dsn: str,
    tables: Sequence[Table] = (),
    count: int = 2,
) -> AsyncIterator[tuple[AsyncSession, ...]]:
    """Несколько независимых сессий, каждая на своём соединении.

    Это рабочий инструмент для проверок конкурентности: две сессии — две
    транзакции, между которыми PostgreSQL действительно расставляет блокировки.
    """
    async with postgres_engine(dsn, tables) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        sessions = [maker() for _ in range(count)]
        try:
            yield tuple(sessions)
        finally:
            for session in sessions:
                with contextlib.suppress(Exception):
                    await session.rollback()
                await session.close()


async def lock_waiter_appeared(session: AsyncSession, timeout: float = 5.0, poll: float = 0.02) -> bool:
    """Дождалась ли база сессии, стоящей в очереди за блокировкой.

    Возвращает результат, а не бросает исключение. Это важно для тестов, где
    главное утверждение — денежное: если синхронизация сама роняет тест, из
    отчёта пропадает то, ради чего он написан («баланс зачислен дважды»), и
    остаётся жалоба инструментовки. Пусть тест дойдёт до конца и скажет по
    существу.

    Наблюдатель должен быть ТРЕТЬЕЙ сессией: держатель занят, ожидающий стоит.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    query = text(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'"
    )
    while loop.time() < deadline:
        result = await session.execute(query)
        if (result.scalar() or 0) > 0:
            return True
        # Наблюдатель не должен держать снимок: иначе он сам мешает уборке.
        await session.rollback()
        await asyncio.sleep(poll)

    return False


async def wait_for_lock_waiter(session: AsyncSession, timeout: float = 5.0, poll: float = 0.02) -> None:
    """То же, но отсутствие соперника — сразу падение теста.

    Годится там, где конкуренция и есть предмет проверки: если её не возникло,
    тест ничего не проверил и обязан это сказать.
    """
    if not await lock_waiter_appeared(session, timeout=timeout, poll=poll):
        raise AssertionError(f'за {timeout} с никто не встал в очередь за блокировкой — конкуренции не возникло')
