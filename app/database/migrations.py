"""Programmatic Alembic migration runner for bot startup."""

from pathlib import Path

import structlog
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect


logger = structlog.get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _PROJECT_ROOT / 'alembic.ini'


def _get_alembic_config() -> Config:
    """Build Alembic Config pointing at the project root."""
    from app.config import settings

    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option('sqlalchemy.url', settings.get_database_url())
    return cfg


async def _detect_db_state() -> str:
    """Detect database state: 'fresh', 'legacy', or 'managed'.

    - fresh: no tables at all — brand new database
    - legacy: has tables but no alembic_version (transition from universal_migration)
    - managed: has alembic_version — already managed by Alembic
    """
    from app.database.database import engine

    async with engine.connect() as conn:
        has_alembic = await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table('alembic_version'))
        if has_alembic:
            return 'managed'
        has_users = await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table('users'))
        return 'legacy' if has_users else 'fresh'


_INITIAL_REVISION = '0001'


async def _bootstrap_fresh_db() -> None:
    """Bootstrap a fresh database: create all tables from models and stamp at head.

    On a fresh DB, running all migrations sequentially would fail because
    migration 0001 uses Base.metadata.create_all() which creates ALL tables
    from the current models.py (including columns/constraints/indexes added
    by later migrations), and then those later migrations try to re-create
    the same objects.  Instead, we create the full schema directly and stamp
    the migration history at HEAD so Alembic considers all migrations applied.
    """
    from app.database.database import engine
    from app.database.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info('Свежая БД: все таблицы созданы из моделей')


async def run_alembic_upgrade() -> None:
    """Run ``alembic upgrade head``, handling fresh and legacy databases."""
    import asyncio

    db_state = await _detect_db_state()

    if db_state == 'fresh':
        logger.warning('Обнаружена пустая БД — создание схемы из моделей + stamp head')
        await _bootstrap_fresh_db()
        await _stamp_alembic_revision('head')
        return

    if db_state == 'legacy':
        logger.warning(
            'Обнаружена существующая БД без alembic_version — автоматический stamp 0001 (переход с universal_migration)'
        )
        await _stamp_alembic_revision(_INITIAL_REVISION)

    cfg = _get_alembic_config()
    loop = asyncio.get_running_loop()
    # run_in_executor offloads to a thread where env.py can safely
    # call asyncio.run() to create its own event loop.
    await loop.run_in_executor(None, command.upgrade, cfg, 'head')
    logger.info('Alembic миграции применены')


async def stamp_alembic_head() -> None:
    """Stamp the DB as being at head without running migrations (for existing DBs)."""
    await _stamp_alembic_revision('head')


async def _stamp_alembic_revision(revision: str) -> None:
    """Stamp the DB at a specific revision without running migrations."""
    import asyncio

    cfg = _get_alembic_config()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, command.stamp, cfg, revision)
    logger.info('Alembic: база отмечена как актуальная', revision=revision)
