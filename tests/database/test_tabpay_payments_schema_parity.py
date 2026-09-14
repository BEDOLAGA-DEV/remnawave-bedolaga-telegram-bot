"""Свежая установка и обновлённая обязаны прийти к одной схеме tabpay_payments.

Свежая база создаётся ``Base.metadata.create_all`` по модели, обновлённая —
миграцией 0113. Расхождение между ними живёт долго и тихо: autogenerate вечно
показывает фантомную разницу, а забытая в миграции колонка (``processed_events``
хранит уже обработанные пары (id, status)) ломает идемпотентность вебхука
только на обновлённых установках.

Проверяется через SQLite: диалект другой, но состав колонок, их типы, индексы и
внешние ключи — то, что расходится, — от него не зависит.
"""

import importlib.util
import pathlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.models import Base, TabPayPayment


VERSIONS = pathlib.Path(__file__).resolve().parents[2] / 'migrations/alembic/versions'
MIGRATION = '0113_create_tabpay_payments.py'
TABLE = 'tabpay_payments'


def _load_migration():
    spec = importlib.util.spec_from_file_location('m0113', VERSIONS / MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_referenced_tables(conn) -> None:
    """Минимальные users/transactions: на них смотрят внешние ключи платежа."""
    conn.execute(sa.text('CREATE TABLE users (id INTEGER PRIMARY KEY)'))
    conn.execute(sa.text('CREATE TABLE transactions (id INTEGER PRIMARY KEY)'))


def _fresh_install(path: pathlib.Path):
    engine = sa.create_engine(f'sqlite:///{path}')
    with engine.begin() as conn:
        _create_referenced_tables(conn)
    Base.metadata.create_all(engine, tables=[TabPayPayment.__table__], checkfirst=True)
    return engine


def _upgraded_install(path: pathlib.Path):
    engine = sa.create_engine(f'sqlite:///{path}')
    with engine.begin() as conn:
        _create_referenced_tables(conn)

    module = _load_migration()
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


def test_columns_match(both):
    fresh, upgraded = both
    fresh_cols = {c['name'] for c in fresh.get_columns(TABLE)}
    upgraded_cols = {c['name'] for c in upgraded.get_columns(TABLE)}

    assert fresh_cols == upgraded_cols, (
        f'только в свежей: {fresh_cols - upgraded_cols}, только в обновлённой: {upgraded_cols - fresh_cols}'
    )


def test_indexes_match(both):
    fresh, upgraded = both
    fresh_idx = {i['name'] for i in fresh.get_indexes(TABLE)}
    upgraded_idx = {i['name'] for i in upgraded.get_indexes(TABLE)}

    assert fresh_idx == upgraded_idx, (
        f'только в свежей: {fresh_idx - upgraded_idx}, только в обновлённой: {upgraded_idx - fresh_idx}'
    )


def _types(inspector) -> dict[str, str]:
    return {c['name']: str(c['type']).upper() for c in inspector.get_columns(TABLE)}


def test_column_types_match(both):
    """Integer вместо Boolean в рукописном DDL иначе не заметить."""
    fresh, upgraded = both
    fresh_types, upgraded_types = _types(fresh), _types(upgraded)

    mismatched = [
        f'{name}: свежая={fresh_types[name]} обновлённая={upgraded_types.get(name)}'
        for name in sorted(fresh_types)
        if fresh_types[name] != upgraded_types.get(name)
    ]

    assert mismatched == [], 'колонки описаны по-разному\n' + '\n'.join(mismatched)


def test_order_id_is_unique(both):
    """Уникальность orderId не даёт двум записям претендовать на один вебхук."""
    for inspector, label in zip(both, ('свежая', 'обновлённая'), strict=True):
        unique_columns = [tuple(index['column_names']) for index in inspector.get_indexes(TABLE) if index.get('unique')]
        unique_columns += [tuple(uc['column_names']) for uc in inspector.get_unique_constraints(TABLE)]

        assert ('order_id',) in unique_columns, f'{label}: order_id не уникален'
        assert ('tabpay_payment_id',) in unique_columns, f'{label}: tabpay_payment_id не уникален'


def test_downgrade_removes_the_table(tmp_path):
    """Откат обязан снимать таблицу, иначе повторный upgrade упрётся в неё."""
    engine = _upgraded_install(tmp_path / 'roundtrip.db')

    module = _load_migration()
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            module.downgrade()

    assert TABLE not in sa.inspect(engine).get_table_names()
