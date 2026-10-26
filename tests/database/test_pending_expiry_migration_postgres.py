"""Nullable durable expiry migrates existing data without inventing paid rights."""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.models import Base, Subscription, Transaction, User
from tests.database.test_retired_gateway_migration_postgres import _load
from tests.fixtures.postgres_db import postgres_engine


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('existing', [True, False])
async def test_pending_expiry_migration_preserves_existing_rights(postgres_database, existing):
    migration = _load('0133_pending_panel_expiry.py')
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        async with engine.begin() as conn:

            def exercise(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    expiry = datetime.now(UTC) + timedelta(days=30)
                    sync.execute(User.__table__.insert().values(id=1, balance_kopeks=50000))
                    sync.execute(Subscription.__table__.insert().values(id=1, user_id=1, end_date=expiry))
                    sync.execute(
                        Transaction.__table__.insert().values(
                            user_id=1, type='subscription_payment', amount_kopeks=-9900
                        )
                    )
                    if existing:
                        migration.downgrade()
                    before = sync.execute(sa.select(Transaction.__table__)).all()
                    migration.upgrade()
                    migration.upgrade()
                    assert sync.scalar(sa.select(Subscription.end_date)) == expiry
                    assert sync.scalar(sa.select(Subscription.pending_panel_expire_at)) is None
                    assert sync.scalar(sa.select(User.balance_kopeks)) == 50000
                    assert sync.execute(sa.select(Transaction.__table__)).all() == before
                    migration.downgrade()
                    assert 'pending_panel_expire_at' not in {
                        c['name'] for c in sa.inspect(sync).get_columns('subscriptions')
                    }
                    migration.upgrade()

            await conn.run_sync(exercise)
