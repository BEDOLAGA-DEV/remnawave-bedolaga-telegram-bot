import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_cispay_sqlite_upgrade_downgrade_preserves_existing_rows():
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        for table in ('users', 'subscriptions', 'transactions'):
            connection.execute(text(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)'))
            connection.execute(text(f'INSERT INTO {table} (id) VALUES (1)'))
        migration = importlib.import_module('migrations.alembic.versions.0140_cispay_subscriptions')
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert 'cispay_subscriptions' in inspect(connection).get_table_names()
            migration.downgrade()
            migration.upgrade()
        assert connection.scalar(text('SELECT id FROM users')) == 1
    engine.dispose()
