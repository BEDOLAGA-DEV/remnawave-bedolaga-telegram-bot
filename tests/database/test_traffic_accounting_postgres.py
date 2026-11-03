"""Real row locks and safe additive migration for traffic accounting."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.models import Base, Subscription, TrafficAccountingState, User
from app.services.traffic_accounting import AccountingConflict, Allowance, TrafficAccount, observe_usage
from app.services.traffic_accounting_store import initialize_account, load_account, save_account
from tests.database.test_retired_gateway_migration_postgres import _load
from tests.fixtures.postgres_db import lock_waiter_appeared, postgres_engine, postgres_sessions


pytestmark = pytest.mark.postgres
NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def seed(db):
    db.add(User(id=1))
    db.add(Subscription(id=1, user_id=1, start_date=NOW, end_date=NOW + timedelta(days=30)))
    await db.commit()
    account = TrafficAccount('123', 0, 0, (Allowance('base:jan', 100, NOW + timedelta(days=30)),))
    await initialize_account(db, 1, account, strategy='MONTH', timezone='UTC', anchor=NOW, next_refresh_at=None)
    await db.commit()
    return account


async def test_concurrent_stale_writer_cannot_restore_consumed_bytes(postgres_database):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=3) as (
        first,
        second,
        observer,
    ):
        original = await seed(first)
        await save_account(first, 1, observe_usage(original, '123', 50, 50, NOW), expected_revision=0)
        task = asyncio.create_task(save_account(second, 1, original, expected_revision=0))
        try:
            assert await lock_waiter_appeared(observer, timeout=2)
            await first.commit()
            with pytest.raises(AccountingConflict):
                await asyncio.wait_for(task, 5)
            await second.rollback()
            loaded, state = await load_account(first, 1)
            assert loaded.remaining_bytes(NOW) == 50
            assert state.revision == 1
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_migration_does_not_touch_existing_quota_and_prevents_lossy_downgrade(postgres_database):
    migration = _load('0139_traffic_accounting.py')
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        async with engine.begin() as connection:

            def exercise(sync):
                sync.execute(User.__table__.insert().values(id=1))
                sync.execute(
                    Subscription.__table__.insert().values(
                        id=1,
                        user_id=1,
                        start_date=NOW,
                        end_date=NOW + timedelta(days=30),
                        traffic_limit_gb=80,
                        traffic_used_gb=60,
                    )
                )
                with Operations.context(MigrationContext.configure(sync)):
                    migration.downgrade()
                    migration.upgrade()
                    migration.upgrade()
                    assert sync.scalar(sa.select(Subscription.traffic_limit_gb)) == 80
                    assert sync.scalar(sa.select(Subscription.traffic_used_gb)) == 60
                    sync.execute(
                        TrafficAccountingState.__table__.insert().values(
                            subscription_id=1,
                            panel_identity='123',
                            lifetime_bytes=60,
                            used_bytes=60,
                            revision=0,
                            phase='prepared',
                            business_strategy='MONTH',
                            calendar_timezone='UTC',
                            calendar_anchor=NOW,
                        )
                    )
                    with pytest.raises(RuntimeError, match='accounting balances'):
                        migration.downgrade()

            await connection.run_sync(exercise)
