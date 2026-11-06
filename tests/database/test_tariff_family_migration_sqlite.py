"""Проверка миграции семейства на изолированной временной базе."""

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from tests.database.test_retired_gateway_migration_postgres import _load


def test_migration_preserves_existing_rows_and_rejects_loss():
    migration = _load('0141_tariff_entitlement_family.py')
    engine = sa.create_engine('sqlite://')
    with engine.begin() as connection:
        connection.execute(sa.text('CREATE TABLE tariffs (id INTEGER PRIMARY KEY, name TEXT NOT NULL)'))
        connection.execute(sa.text("INSERT INTO tariffs (id, name) VALUES (1, 'Тариф')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert connection.scalar(sa.text('SELECT entitlement_family FROM tariffs WHERE id=1')) is None
            connection.execute(sa.text("UPDATE tariffs SET entitlement_family='main' WHERE id=1"))
            with pytest.raises(RuntimeError, match='семейства тарифов'):
                migration.downgrade()
            assert connection.scalar(sa.text('SELECT entitlement_family FROM tariffs WHERE id=1')) == 'main'
            connection.execute(sa.text('UPDATE tariffs SET entitlement_family=NULL WHERE id=1'))
            migration.downgrade()
            assert 'entitlement_family' not in {c['name'] for c in sa.inspect(connection).get_columns('tariffs')}
            assert connection.scalar(sa.text('SELECT name FROM tariffs WHERE id=1')) == 'Тариф'
    engine.dispose()
