"""Both bootstrap and upgrade preserve financial history and constraints."""

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.models import AnorePayment, Base, User
from tests.database.test_retired_gateway_migration_postgres import _load
from tests.fixtures.postgres_db import postgres_engine


pytestmark = pytest.mark.postgres


async def test_anore_upgrade_bootstrap_and_downgrade_guard(postgres_database):
    migration = _load('0135_create_anore_payments.py')
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        async with engine.begin() as conn:

            def exercise(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    baseline = sa.inspect(sync).get_columns('anore_payments')
                    migration.upgrade()  # Already bootstrapped metadata.
                    migration.downgrade()
                    assert not sa.inspect(sync).has_table('anore_payments')
                    sync.execute(User.__table__.insert().values(id=1, balance_kopeks=123))
                    migration.upgrade()
                    migration.upgrade()
                    upgraded = sa.inspect(sync).get_columns('anore_payments')
                    assert [(c['name'], str(c['type']), c['nullable']) for c in upgraded] == [
                        (c['name'], str(c['type']), c['nullable']) for c in baseline
                    ]
                    sync.execute(AnorePayment.__table__.insert().values(user_id=1, order_id='test', amount_kopeks=100))
                    with pytest.raises(RuntimeError, match='Archive Anore'):
                        migration.downgrade()
                    sync.execute(sa.delete(User).where(User.id == 1))
                    assert sync.scalar(sa.select(AnorePayment.user_id)) is None

            await conn.run_sync(exercise)
