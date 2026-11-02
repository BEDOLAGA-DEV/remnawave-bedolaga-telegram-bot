"""Source selection must use state committed before the wallet lock is acquired."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.database.crud import user as users
from app.database.models import Base, Subscription, Tariff, Transaction, User
from app.services import subscription_auto_purchase_service as auto
from app.services.pricing_engine import pricing_engine
from tests.fixtures.postgres_db import postgres_engine


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('daily', [False, True])
@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('trial', [False, True])
@pytest.mark.parametrize('change', ['delete', 'reassign', 'device_limit'])
async def test_source_is_resolved_after_wallet_lock(postgres_database, monkeypatch, daily, multi, trial, change):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    quote = AsyncMock(side_effect=AssertionError('quote boundary'))
    monkeypatch.setattr(pricing_engine, 'calculate_tariff_purchase_price', quote)
    now = datetime.now(UTC)
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            db.add_all([User(id=1, telegram_id=101, balance_kopeks=10000), User(id=2, telegram_id=202)])
            db.add(
                Tariff(
                    id=1,
                    name='Target',
                    is_active=True,
                    is_daily=daily,
                    daily_price_kopeks=100,
                    device_limit=1,
                    max_device_limit=10,
                    period_prices={'30': 300},
                )
            )
            db.add_all(
                [
                    Subscription(
                        id=10,
                        user_id=1,
                        tariff_id=None,
                        status='active',
                        is_trial=trial,
                        remnawave_short_id='source',
                        device_limit=3,
                        end_date=now + timedelta(days=50),
                    ),
                    Subscription(
                        id=20,
                        user_id=1,
                        tariff_id=None,
                        status='active',
                        is_trial=trial,
                        remnawave_short_id='neighbor',
                        device_limit=7,
                        end_date=now + timedelta(days=10),
                    ),
                ]
            )
            await db.commit()
        lock_user = users.lock_user_for_pricing
        entered = False

        async def lock_after_committed_change(db, user_id):
            nonlocal entered
            assert not entered
            entered = True
            # A separate transaction wins the wallet lock while checkout waits.
            async with maker() as writer:
                await lock_user(writer, user_id)
                if change == 'delete':
                    await writer.execute(delete(Subscription).where(Subscription.id == 10))
                else:
                    values = {'user_id': 2} if change == 'reassign' else {'device_limit': 5}
                    await writer.execute(update(Subscription).where(Subscription.id == 10).values(**values))
                await writer.commit()
            return await lock_user(db, user_id)

        monkeypatch.setattr(users, 'lock_user_for_pricing', lock_after_committed_change)
        async with maker() as db:
            user = await users.get_user_by_id(db, 1)
            purchase = auto._auto_purchase_daily_tariff if daily else auto._auto_purchase_tariff
            cart = {'tariff_id': 1, 'period_days': 30, 'subscription_id': 10}
            # Period single mode selects the current primary row; daily carts
            # already pin explicit owned IDs even with multi mode disabled.
            allowed = change == 'device_limit' or (not daily and not multi)
            if allowed:
                with pytest.raises(AssertionError, match='quote boundary'):
                    await purchase(db, user, cart)
                expected_cap = 1 if trial else (5 if change == 'device_limit' else 7)
                assert quote.await_args.kwargs['device_limit'] == expected_cap
            else:
                assert not await purchase(db, user, cart)
                quote.assert_not_awaited()
            assert entered
            await db.commit()
        async with maker() as observer:
            assert (await observer.get(User, 1)).balance_kopeks == 10000
            assert (await observer.get(User, 2)).balance_kopeks == 0
            assert list(await observer.scalars(select(Transaction))) == []
            source = await observer.get(Subscription, 10)
            if change == 'delete':
                assert source is None
            else:
                assert (source.user_id, source.device_limit, source.tariff_id) == (
                    2 if change == 'reassign' else 1,
                    5 if change == 'device_limit' else 3,
                    None,
                )
            neighbor = await observer.get(Subscription, 20)
            assert (neighbor.user_id, neighbor.device_limit, neighbor.tariff_id) == (1, 7, None)
