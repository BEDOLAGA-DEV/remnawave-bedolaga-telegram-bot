"""Reset delivery migration preserves billing and refuses to erase uncertain work."""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.models import Base, PaidTrafficResetIntent, Subscription, Transaction, User
from tests.database.test_retired_gateway_migration_postgres import _load
from tests.fixtures.postgres_db import postgres_engine


pytestmark = pytest.mark.postgres


async def test_reset_intent_migration_upgrade_downgrade_and_safety(postgres_database):
    migration = _load('0134_paid_traffic_reset_intents.py')
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        async with engine.begin() as conn:

            def exercise(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    migration.downgrade()
                    assert not sa.inspect(sync).has_table('paid_traffic_reset_intents')
                    now = datetime.now(UTC)
                    sync.execute(User.__table__.insert().values(id=1, balance_kopeks=50000))
                    sync.execute(
                        Subscription.__table__.insert().values(id=1, user_id=1, end_date=now + timedelta(days=30))
                    )
                    sync.execute(
                        Transaction.__table__.insert().values(
                            id=1, user_id=1, type='subscription_payment', amount_kopeks=-1000
                        )
                    )
                    migration.upgrade()
                    migration.upgrade()
                    assert sync.scalar(sa.select(sa.func.count()).select_from(PaidTrafficResetIntent)) == 0
                    assert sync.scalar(sa.select(User.balance_kopeks)) == 50000
                    assert sync.scalar(sa.select(Transaction.amount_kopeks)) == -1000
                    sync.execute(
                        PaidTrafficResetIntent.__table__.insert().values(
                            id=1,
                            subscription_id=1,
                            user_id=1,
                            transaction_id=1,
                            operation_key='test:1',
                            charge_at=now,
                            reason='test',
                            state='attempted',
                            attempted_at=now,
                            target_panel_id=2**40,
                            target_short_uuid='masked',
                            baseline_observed=True,
                        )
                    )
                    with pytest.raises(RuntimeError, match='Resolve paid traffic reset intents'):
                        migration.downgrade()
                    sync.execute(sa.delete(Subscription).where(Subscription.id == 1))
                    assert sync.scalar(sa.select(PaidTrafficResetIntent.subscription_id)) is None
                    assert sync.scalar(sa.select(PaidTrafficResetIntent.target_panel_id)) == 2**40
                    sync.execute(sa.update(PaidTrafficResetIntent).values(state='confirmed', confirmed_at=now))
                    migration.downgrade()
                    assert not sa.inspect(sync).has_table('paid_traffic_reset_intents')
                    migration.upgrade()
                    assert sync.scalar(sa.select(User.balance_kopeks)) == 50000
                    assert sync.scalar(sa.select(Transaction.amount_kopeks)) == -1000

            await conn.run_sync(exercise)
