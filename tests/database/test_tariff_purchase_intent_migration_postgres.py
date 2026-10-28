import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.models import Base, TariffPurchaseIntent, User
from tests.database.test_retired_gateway_migration_postgres import _load
from tests.fixtures.postgres_db import postgres_engine


pytestmark = pytest.mark.postgres


async def test_intent_migration_preserves_completed_checkout_identity(postgres_database):
    migration = _load('0137_tariff_purchase_intents.py')
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        async with engine.begin() as connection:

            def exercise(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    migration.downgrade()
                    assert not sa.inspect(sync).has_table('tariff_purchase_intents')
                    migration.upgrade()
                    migration.upgrade()
                    constraints = sa.inspect(sync).get_unique_constraints('tariff_purchase_intents')
                    assert any(item['column_names'] == ['user_id', 'checkout_id'] for item in constraints)
                    sync.execute(User.__table__.insert().values(id=1, balance_kopeks=0))
                    sync.execute(
                        TariffPurchaseIntent.__table__.insert().values(
                            user_id=1,
                            checkout_id='11111111-1111-4111-8111-111111111111',
                            fingerprint='0' * 64,
                            parameters={},
                            charged_kopeks=0,
                        )
                    )
                    with pytest.raises(RuntimeError, match='completed tariff checkout identities'):
                        migration.downgrade()
                    assert sync.scalar(sa.select(sa.func.count()).select_from(TariffPurchaseIntent)) == 1

            await connection.run_sync(exercise)
