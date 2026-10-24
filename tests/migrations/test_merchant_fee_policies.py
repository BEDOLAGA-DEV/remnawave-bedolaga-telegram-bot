import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_upgrade_downgrade_preserves_existing_configuration():
    path = Path(__file__).parents[2] / 'migrations/alembic/versions/0134_merchant_fee_policies.py'
    spec = importlib.util.spec_from_file_location('merchant_fee_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine('sqlite://')
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE payment_method_configs (id INTEGER PRIMARY KEY, display_name TEXT)')
        connection.exec_driver_sql("INSERT INTO payment_method_configs VALUES (1, 'Gateway')")
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert (
                connection.exec_driver_sql('SELECT merchant_fee_policies FROM payment_method_configs').scalar() is None
            )
            migration.downgrade()
            assert connection.exec_driver_sql('SELECT display_name FROM payment_method_configs').scalar() == 'Gateway'
    engine.dispose()
