"""Проверка сохранения данных при миграции семейства тарифов."""

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.models import Base, Tariff
from tests.database.test_retired_gateway_migration_postgres import _load
from tests.fixtures.postgres_db import postgres_engine


pytestmark = pytest.mark.postgres


async def test_family_migration_preserves_values_and_blocks_loss(postgres_database):
    migration = _load('0141_tariff_entitlement_family.py')
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        async with engine.begin() as connection:

            def exercise(sync):
                sync.execute(Tariff.__table__.insert().values(id=1, name='Тариф'))
                with Operations.context(MigrationContext.configure(sync)):
                    migration.downgrade()
                    migration.upgrade()
                    assert sync.scalar(sa.text('SELECT entitlement_family FROM tariffs WHERE id=1')) is None
                    sync.execute(sa.text("UPDATE tariffs SET entitlement_family='main' WHERE id=1"))
                    with pytest.raises(RuntimeError, match='семейства тарифов'):
                        migration.downgrade()
                    assert sync.scalar(sa.text('SELECT entitlement_family FROM tariffs WHERE id=1')) == 'main'
                    sync.execute(sa.text('UPDATE tariffs SET entitlement_family=NULL WHERE id=1'))
                    migration.downgrade()
                    assert 'entitlement_family' not in {c['name'] for c in sa.inspect(sync).get_columns('tariffs')}
                    migration.upgrade()

            await connection.run_sync(exercise)
