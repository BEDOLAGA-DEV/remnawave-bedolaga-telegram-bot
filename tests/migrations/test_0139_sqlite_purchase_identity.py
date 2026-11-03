import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_sqlite_upgrade_preserves_purchase_rows_indexes_and_nonreuse():
    path = Path('migrations/alembic/versions/0139_traffic_accounting.py')
    spec = importlib.util.spec_from_file_location('traffic_0139', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE subscriptions(id INTEGER PRIMARY KEY)')
        connection.exec_driver_sql(
            'CREATE TABLE traffic_purchases(id INTEGER PRIMARY KEY, subscription_id INTEGER NOT NULL REFERENCES subscriptions(id), traffic_gb INTEGER NOT NULL, expires_at DATETIME NOT NULL, created_at DATETIME)'
        )
        connection.exec_driver_sql(
            'CREATE INDEX custom_purchase_expiry ON traffic_purchases(subscription_id, expires_at)'
        )
        connection.exec_driver_sql('INSERT INTO subscriptions(id) VALUES(1)')
        connection.exec_driver_sql("INSERT INTO traffic_purchases VALUES(17,1,40,'2026-01-01','2025-12-01')")
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert connection.exec_driver_sql('SELECT id,traffic_gb FROM traffic_purchases').all() == [(17, 40)]
        assert 'custom_purchase_expiry' in {i['name'] for i in sa.inspect(connection).get_indexes('traffic_purchases')}
        connection.exec_driver_sql('DELETE FROM traffic_purchases')
        connection.exec_driver_sql(
            "INSERT INTO traffic_purchases(subscription_id,traffic_gb,expires_at) VALUES(1,40,'2026-02-01')"
        )
        assert connection.exec_driver_sql('SELECT id FROM traffic_purchases').scalar_one() > 17
    engine.dispose()


def test_sqlite_upgrade_preserves_transaction_links_and_reserved_history_ids():
    path = Path('migrations/alembic/versions/0139_traffic_accounting.py')
    spec = importlib.util.spec_from_file_location('traffic_0139_transactions', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE users(id INTEGER PRIMARY KEY)')
        connection.exec_driver_sql('CREATE TABLE subscriptions(id INTEGER PRIMARY KEY)')
        connection.exec_driver_sql(
            'CREATE TABLE transactions(id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), external_id TEXT UNIQUE, amount INTEGER)'
        )
        connection.exec_driver_sql('CREATE INDEX custom_transaction_amount ON transactions(user_id, amount)')
        connection.exec_driver_sql(
            'CREATE TABLE payments(id INTEGER PRIMARY KEY, transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL)'
        )
        connection.exec_driver_sql('INSERT INTO users VALUES(1)')
        connection.exec_driver_sql('INSERT INTO subscriptions VALUES(1)')
        connection.exec_driver_sql("INSERT INTO transactions VALUES(17,1,'masked-17',100)")
        connection.exec_driver_sql('INSERT INTO payments VALUES(1,17),(2,91)')
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert connection.exec_driver_sql('SELECT * FROM transactions').all() == [(17, 1, 'masked-17', 100)]
        assert connection.exec_driver_sql('SELECT transaction_id FROM payments ORDER BY id').scalars().all() == [17, 91]
        inspector = sa.inspect(connection)
        assert 'custom_transaction_amount' in {i['name'] for i in inspector.get_indexes('transactions')}
        assert inspector.get_foreign_keys('transactions')[0]['referred_table'] == 'users'
        assert inspector.get_unique_constraints('transactions')[0]['column_names'] == ['external_id']
        assert (
            connection.exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name='transactions'").scalar_one() == 91
        )
        connection.exec_driver_sql(
            "INSERT INTO traffic_accounting_states(subscription_id,panel_identity,lifetime_bytes,used_bytes,revision,phase,business_strategy,calendar_timezone,calendar_anchor) VALUES(1,'masked',0,0,0,'ready','NO_RESET','UTC','2026-01-01')"
        )
        connection.exec_driver_sql(
            "INSERT INTO traffic_allowances VALUES(1,'base:tariff:user-switch:501',100,0,'2026-02-01')"
        )
        connection.exec_driver_sql(
            "INSERT INTO traffic_allowances VALUES(1,'base:tariff:classic-traffic:11111111111111111111111111111111',100,0,'2026-02-01')"
        )
        connection.exec_driver_sql(
            "INSERT INTO traffic_base_grant_intents(subscription_id,operation_key,base_bytes,business_strategy,requested_at,expires_at,state) VALUES(1,'platega:payment:701',100,'NO_RESET','2026-01-01','2026-02-01','applied')"
        )
        connection.exec_driver_sql('DELETE FROM transactions')
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        connection.exec_driver_sql("INSERT INTO transactions(user_id, external_id, amount) VALUES(1,'masked-new',100)")
        assert connection.exec_driver_sql('SELECT id FROM transactions').scalar_one() == 702
    engine.dispose()


def test_sqlite_rebuild_refuses_enabled_foreign_keys_before_any_ddl():
    import pytest

    path = Path('migrations/alembic/versions/0139_traffic_accounting.py')
    spec = importlib.util.spec_from_file_location('traffic_0139_safe_foreign_keys', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        connection.exec_driver_sql('PRAGMA foreign_keys=ON')
        connection.exec_driver_sql('CREATE TABLE subscriptions(id INTEGER PRIMARY KEY)')
        connection.exec_driver_sql('CREATE TABLE transactions(id INTEGER PRIMARY KEY)')
        with Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError, match='foreign_keys=OFF'):
                migration.upgrade()
        assert [column['name'] for column in sa.inspect(connection).get_columns('subscriptions')] == ['id']
        assert (
            'AUTOINCREMENT'
            not in connection.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='transactions'").scalar_one()
        )
    engine.dispose()
