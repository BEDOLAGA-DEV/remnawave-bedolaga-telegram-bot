import asyncio
import sys
from pathlib import Path
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

sys.path.append(str(Path(__file__).parent.parent.parent))

from app.database.models import Base
from app.config import settings

config = context.config

# Only apply fileConfig when running via CLI (make migrate, make migration).
# When called programmatically from run_alembic_upgrade(), structlog is already
# configured — fileConfig would replace root logger handlers and break logging.
import logging as _logging

if config.config_file_name is not None and not _logging.root.handlers:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# URL also set in app/database/migrations.py for programmatic usage;
# this line is needed for CLI invocation (make migrate, make migration).
config.set_main_option('sqlalchemy.url', settings.get_database_url())


def run_migrations_offline() -> None:
    url = config.get_main_option('sqlalchemy.url')
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={'paramstyle': 'named'},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix='sqlalchemy.',
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    # asyncio.run() is safe here: when called programmatically via
    # run_alembic_upgrade(), this runs inside run_in_executor() which
    # creates a separate thread with no event loop, so asyncio.run()
    # can create a fresh loop without conflict.
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
