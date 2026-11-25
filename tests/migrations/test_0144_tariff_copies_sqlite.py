"""Миграция 0144: копии тарифа рядом с обычной подпиской, откат — только без живых копий."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _migration():
    path = Path('migrations/alembic/versions/0144_subscription_tariff_copies.py')
    spec = importlib.util.spec_from_file_location('tariff_copies_0144', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _old_schema(connection):
    connection.exec_driver_sql(
        'CREATE TABLE subscriptions(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, tariff_id INTEGER, '
        'status VARCHAR(20) NOT NULL)'
    )
    connection.exec_driver_sql(
        'CREATE UNIQUE INDEX uq_subscriptions_user_tariff_active ON subscriptions (user_id, tariff_id) '
        "WHERE tariff_id IS NOT NULL AND status IN ('active', 'trial', 'limited')"
    )
    connection.exec_driver_sql("INSERT INTO subscriptions VALUES (1, 7, 3, 'active'), (2, 7, 3, 'expired')")


def _run(connection, step):
    with Operations.context(MigrationContext.configure(connection)):
        step()


def test_upgrade_lets_copies_live_next_to_the_ordinary_subscription():
    migration = _migration()
    engine = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        _old_schema(connection)
        _run(connection, migration.upgrade)

        assert connection.exec_driver_sql('SELECT id, status FROM subscriptions ORDER BY id').all() == [
            (1, 'active'),
            (2, 'expired'),
        ]
        connection.exec_driver_sql("INSERT INTO subscriptions VALUES (3, 7, 3, 'active', 'key-a')")
        connection.exec_driver_sql("INSERT INTO subscriptions VALUES (4, 7, 3, 'active', 'key-b')")
        with pytest.raises(sa.exc.IntegrityError):
            # Вторая живая обычная подписка тарифа по-прежнему запрещена.
            connection.exec_driver_sql("INSERT INTO subscriptions VALUES (5, 7, 3, 'active', NULL)")
        with pytest.raises(sa.exc.IntegrityError):
            # Повтор той же покупки копии упирается в её ключ.
            connection.exec_driver_sql("INSERT INTO subscriptions VALUES (6, 7, 3, 'expired', 'key-a')")
    engine.dispose()


def test_upgrade_is_repeatable():
    migration = _migration()
    engine = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        _old_schema(connection)
        _run(connection, migration.upgrade)
        _run(connection, migration.upgrade)
        indexes = {index['name'] for index in sa.inspect(connection).get_indexes('subscriptions')}
        assert {'uq_subscriptions_user_tariff_active', 'uq_subscriptions_copy_key'} <= indexes
    engine.dispose()


def test_downgrade_refuses_while_live_copies_exist():
    migration = _migration()
    engine = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        _old_schema(connection)
        _run(connection, migration.upgrade)
        connection.exec_driver_sql("INSERT INTO subscriptions VALUES (3, 7, 3, 'active', 'key-a')")
        with pytest.raises(RuntimeError, match='live tariff copies'):
            _run(connection, migration.downgrade)
    engine.dispose()


def test_downgrade_restores_one_live_subscription_per_tariff():
    migration = _migration()
    engine = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        _old_schema(connection)
        _run(connection, migration.upgrade)
        connection.exec_driver_sql("INSERT INTO subscriptions VALUES (3, 7, 3, 'expired', 'key-a')")
        _run(connection, migration.downgrade)

        columns = {column['name'] for column in sa.inspect(connection).get_columns('subscriptions')}
        assert 'copy_key' not in columns
        assert connection.exec_driver_sql('SELECT COUNT(*) FROM subscriptions').scalar_one() == 3
        with pytest.raises(sa.exc.IntegrityError):
            connection.exec_driver_sql("INSERT INTO subscriptions VALUES (4, 7, 3, 'active')")
    engine.dispose()
