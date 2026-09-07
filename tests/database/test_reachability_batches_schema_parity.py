"""Свежая установка и обновлённая обязаны прийти к одной схеме пачек проверок.

Свежая база создаётся ``Base.metadata.create_all`` по модели, обновлённая — миграциями
0115 и 0116 подряд. Сверяются и новая таблица ``reachability_batches``, и колонка
``batch_id`` с индексом у ``reachability_jobs``: забытая колонка в миграции ломает запуск
пачки только на обновлённых установках.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.models import Base, ReachabilityBatch, ReachabilityJob, ReachabilityLeg, ReachabilityTargetPref


VERSIONS = pathlib.Path(__file__).resolve().parents[2] / 'migrations/alembic/versions'
MIGRATIONS = ('0115_create_reachability_tables.py', '0116_reachability_batches.py')
TABLES = ('reachability_batches', 'reachability_jobs')


def _load_migration(name: str):
    spec = importlib.util.spec_from_file_location(name.split('_')[0], VERSIONS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_referenced_tables(conn) -> None:
    conn.execute(sa.text('CREATE TABLE users (id INTEGER PRIMARY KEY)'))


def _fresh_install(path: pathlib.Path):
    engine = sa.create_engine(f'sqlite:///{path}')
    with engine.begin() as conn:
        _create_referenced_tables(conn)
    tables = [
        ReachabilityBatch.__table__,
        ReachabilityJob.__table__,
        ReachabilityLeg.__table__,
        ReachabilityTargetPref.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables, checkfirst=True)
    return engine


def _upgraded_install(path: pathlib.Path):
    engine = sa.create_engine(f'sqlite:///{path}')
    with engine.begin() as conn:
        _create_referenced_tables(conn)
    for name in MIGRATIONS:
        module = _load_migration(name)
        with engine.begin() as conn:
            context = MigrationContext.configure(conn)
            with Operations.context(context):
                module.upgrade()
    return engine


@pytest.fixture
def both(tmp_path):
    fresh = _fresh_install(tmp_path / 'fresh.db')
    upgraded = _upgraded_install(tmp_path / 'upgraded.db')
    return sa.inspect(fresh), sa.inspect(upgraded)


@pytest.mark.parametrize('table', TABLES)
def test_columns_match(both, table: str) -> None:
    fresh, upgraded = both
    fresh_cols = {c['name'] for c in fresh.get_columns(table)}
    upgraded_cols = {c['name'] for c in upgraded.get_columns(table)}
    assert fresh_cols == upgraded_cols, (
        f'{table}: только в свежей {fresh_cols - upgraded_cols}, только в обновлённой {upgraded_cols - fresh_cols}'
    )


@pytest.mark.parametrize('table', TABLES)
def test_indexes_match(both, table: str) -> None:
    fresh, upgraded = both
    fresh_idx = {i['name'] for i in fresh.get_indexes(table)}
    upgraded_idx = {i['name'] for i in upgraded.get_indexes(table)}
    assert fresh_idx == upgraded_idx, (
        f'{table}: только в свежей {fresh_idx - upgraded_idx}, только в обновлённой {upgraded_idx - fresh_idx}'
    )


@pytest.mark.parametrize('table', TABLES)
def test_column_types_match(both, table: str) -> None:
    fresh, upgraded = both
    fresh_types = {c['name']: str(c['type']).upper() for c in fresh.get_columns(table)}
    upgraded_types = {c['name']: str(c['type']).upper() for c in upgraded.get_columns(table)}
    mismatched = [
        f'{name}: свежая={fresh_types[name]} обновлённая={upgraded_types.get(name)}'
        for name in sorted(fresh_types)
        if fresh_types[name] != upgraded_types.get(name)
    ]
    assert mismatched == [], f'{table}: колонки описаны по-разному\n' + '\n'.join(mismatched)


def test_jobs_reference_batches(both) -> None:
    for inspector, label in zip(both, ('свежая', 'обновлённая'), strict=True):
        fks = [
            (fk['constrained_columns'], fk['referred_table']) for fk in inspector.get_foreign_keys('reachability_jobs')
        ]
        assert (['batch_id'], 'reachability_batches') in fks, f'{label}: у задачи нет ссылки на пачку'


def test_downgrade_removes_batches(tmp_path) -> None:
    engine = _upgraded_install(tmp_path / 'roundtrip.db')
    module = _load_migration(MIGRATIONS[1])
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            module.downgrade()
    inspector = sa.inspect(engine)
    assert 'reachability_batches' not in inspector.get_table_names()
    assert 'batch_id' not in {c['name'] for c in inspector.get_columns('reachability_jobs')}
