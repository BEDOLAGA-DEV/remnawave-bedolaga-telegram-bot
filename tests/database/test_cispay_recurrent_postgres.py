import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.database.models import Base, Subscription, User
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres
CHECKOUT = '00000000-0000-4000-8000-000000000001'
SNAPSHOT = {'tariff_id': None, 'device_limit': 2, 'amount_kopeks': 10000, 'period_days': 30}


async def seed(db):
    from datetime import UTC, datetime

    db.add(User(id=1, telegram_id=1, status='active'))
    await db.flush()
    db.add(
        Subscription(
            id=1,
            user_id=1,
            status='expired',
            is_trial=False,
            device_limit=2,
            start_date=datetime.now(UTC),
            end_date=datetime.now(UTC),
        )
    )
    await db.commit()


async def test_concurrent_checkout_reserves_one_agreement(postgres_database):
    from app.database.crud.cispay_subscriptions import reserve_agreement
    from app.database.models import CisPaySubscription

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (first, second):
        await seed(first)

        async def reserve(db):
            return (
                await reserve_agreement(
                    db, user_id=1, subscription_id=1, shop_id='shop', checkout_id=CHECKOUT, snapshot=SNAPSHOT
                )
            ).id

        ids = await asyncio.gather(reserve(first), reserve(second))
        assert ids[0] == ids[1]
        assert len((await first.scalars(select(CisPaySubscription))).all()) == 1


async def test_conflicting_retry_does_not_change_consent(postgres_database):
    from app.database.crud.cispay_subscriptions import reserve_agreement

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        await reserve_agreement(
            db, user_id=1, subscription_id=1, shop_id='shop', checkout_id=CHECKOUT, snapshot=SNAPSHOT
        )
        with pytest.raises(ValueError):
            await reserve_agreement(
                db,
                user_id=1,
                subscription_id=1,
                shop_id='shop',
                checkout_id=CHECKOUT,
                snapshot=SNAPSHOT | {'amount_kopeks': 20000},
            )


async def test_timeout_does_not_repeat_create(postgres_database, monkeypatch):
    from app.config import settings
    from app.database.crud.cispay_subscriptions import reserve_agreement
    from app.database.models import CisPaySubscription
    from app.services.cispay_recurrent_service import create_cispay_agreement
    from app.services.cispay_service import CisPayAPIError, cispay_service

    monkeypatch.setattr(settings, 'CISPAY_SHOP_ID', 'shop')
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'test-key')
    create = AsyncMock(side_effect=CisPayAPIError())
    monkeypatch.setattr(cispay_service, 'create_subscription', create)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        agreement = await reserve_agreement(
            db, user_id=1, subscription_id=1, shop_id='shop', checkout_id=CHECKOUT, snapshot=SNAPSHOT
        )
        result = await create_cispay_agreement(db, attempt_id=agreement.id)
        assert result['status'] == 'CREATION_UNKNOWN'
        await create_cispay_agreement(db, attempt_id=agreement.id)
        create.assert_awaited_once()
        stored = await db.get(CisPaySubscription, agreement.id)
        assert stored.status == 'CREATION_UNKNOWN'


async def test_migration_up_down_up_preserves_existing_rows(postgres_database):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    from app.database.models import CisPaySubscription

    tables = [table for table in Base.metadata.sorted_tables if not table.name.startswith('cispay_subscription')]
    async with postgres_sessions(postgres_database, tables) as (db, _):
        await seed(db)
        connection = await db.connection()

        def migrate(sync):
            migration = importlib.import_module('migrations.alembic.versions.0140_cispay_subscriptions')
            with Operations.context(MigrationContext.configure(sync)):
                migration.downgrade()  # Fixture provisions the complete application schema.
                migration.upgrade()
                columns = {column['name'] for column in inspect(sync).get_columns('cispay_subscriptions')}
                assert columns == set(CisPaySubscription.__table__.columns.keys())
                migration.downgrade()
                migration.upgrade()

        await connection.run_sync(migrate)
        assert (await db.get(User, 1)).telegram_id == 1
        await db.commit()


async def test_different_checkout_cannot_start_during_unknown_create(postgres_database):
    from app.database.crud.cispay_subscriptions import reserve_agreement

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        agreement = await reserve_agreement(
            db, user_id=1, subscription_id=1, shop_id='shop', checkout_id=CHECKOUT, snapshot=SNAPSHOT
        )
        agreement.status = 'CREATION_UNKNOWN'
        await db.commit()
        with pytest.raises(ValueError, match='recurrent_already_exists'):
            await reserve_agreement(
                db,
                user_id=1,
                subscription_id=1,
                shop_id='shop',
                checkout_id='00000000-0000-4000-8000-000000000002',
                snapshot=SNAPSHOT,
            )
