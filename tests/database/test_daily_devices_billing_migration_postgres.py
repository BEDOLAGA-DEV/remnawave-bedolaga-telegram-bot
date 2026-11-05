from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.models import Base, Subscription, User
from tests.database.test_retired_gateway_migration_postgres import _load
from tests.fixtures.postgres_db import postgres_engine


pytestmark = pytest.mark.postgres


async def test_daily_billing_migration_preserves_legacy_and_protects_new_policy(postgres_database):
    migration = _load('0138_daily_selected_devices_billing.py')
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        async with engine.begin() as connection:

            def exercise(sync):
                sync.execute(User.__table__.insert().values(id=1, balance_kopeks=0))
                sync.execute(
                    Subscription.__table__.insert().values(
                        id=1,
                        user_id=1,
                        start_date=datetime.now(UTC),
                        end_date=datetime.now(UTC) + timedelta(days=1),
                        remnawave_short_id='migration-test',
                        device_limit=5,
                    )
                )
                with Operations.context(MigrationContext.configure(sync)):
                    migration.downgrade()
                    migration.upgrade()
                    migration.upgrade()
                    assert (
                        sync.scalar(sa.text('SELECT daily_devices_billing_version FROM subscriptions WHERE id=1')) == 0
                    )
                    sync.execute(sa.text('UPDATE subscriptions SET daily_devices_billing_version=1 WHERE id=1'))
                    with pytest.raises(RuntimeError, match='daily device billing policy'):
                        migration.downgrade()
                    assert sync.scalar(sa.select(Subscription.device_limit).where(Subscription.id == 1)) == 5

            await connection.run_sync(exercise)
