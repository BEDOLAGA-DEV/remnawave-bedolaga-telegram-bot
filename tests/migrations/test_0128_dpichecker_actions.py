"""Миграция 0128: таблица dpichecker_actions на PostgreSQL совпадает с моделью, откат её убирает."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.models import DpiCheckerAction, User
from tests.fixtures.postgres_db import postgres_engine


MIGRATION = Path('migrations/alembic/versions/0128_dpichecker_actions.py')


def _migration():
    spec = importlib.util.spec_from_file_location('m0128', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    module = _migration()
    assert (module.revision, module.down_revision) == ('0128', '0127')


def _run(connection, step: str) -> dict:
    module = _migration()
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        getattr(module, step)()
    inspector = sa.inspect(connection)
    if 'dpichecker_actions' not in inspector.get_table_names():
        return {}
    return {
        'columns': {column['name'] for column in inspector.get_columns('dpichecker_actions')},
        'unique': {tuple(u['column_names']) for u in inspector.get_unique_constraints('dpichecker_actions')},
    }


def _restore_model_table(connection) -> None:
    DpiCheckerAction.__table__.create(connection, checkfirst=True)


@pytest.mark.postgres
async def test_upgrade_matches_model_and_downgrade_drops(postgres_database):
    # Схема тестовой базы уже создана по моделям — таблицу сначала убираем, иначе миграция
    # увидит готовую и ничего не проверит; в конце возвращаем для остальных тестов.
    async with postgres_engine(postgres_database, [User.__table__]) as engine:
        async with engine.begin() as connection:
            try:
                await connection.run_sync(_run, 'downgrade')
                created = await connection.run_sync(_run, 'upgrade')
                again = await connection.run_sync(_run, 'upgrade')  # повтор не падает
                dropped = await connection.run_sync(_run, 'downgrade')
            finally:
                await connection.run_sync(_restore_model_table)
    assert created['columns'] == {column.name for column in DpiCheckerAction.__table__.columns}
    assert ('kind', 'remote_id') in created['unique']
    assert again == created
    assert dropped == {}
