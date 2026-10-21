"""Daily activation must retain the wallet lock until product and ledger persist."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.database.crud import subscription as subscriptions, transaction as transactions, user as users
from app.database.models import Base, Subscription, Tariff, Transaction, User
from app.services import subscription_auto_purchase_service as auto
from app.services.subscription_service import SubscriptionService
from tests.fixtures.postgres_db import postgres_engine


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('fresh', [False, True])
@pytest.mark.parametrize('promo', [0, 17, 100])
@pytest.mark.parametrize('failure', [None, 'debit', 'ledger', 'provision'])
async def test_daily_auto_activation_is_atomic(postgres_database, monkeypatch, fresh, promo, failure):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
    monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    now = datetime.now(UTC)
    monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', AsyncMock())
    entered, release, second_debit = asyncio.Event(), asyncio.Event(), asyncio.Event()
    debit = users.subtract_user_balance
    debit_calls = 0

    async def charge(*args, **kwargs):
        nonlocal debit_calls
        debit_calls += 1
        call = debit_calls
        result = await debit(*args, **kwargs)
        if call == 1:
            if failure == 'debit':
                raise RuntimeError('Injected debit boundary failure')
            if failure is None:
                entered.set()
                await asyncio.wait_for(release.wait(), 10)
        else:
            second_debit.set()
        return result

    monkeypatch.setattr(users, 'subtract_user_balance', charge)
    create_transaction = transactions.create_transaction
    ledger_calls = 0

    async def ledger(*args, **kwargs):
        nonlocal ledger_calls
        ledger_calls += 1
        if failure == 'ledger' and ledger_calls == 1:
            raise RuntimeError('Injected daily ledger failure')
        return await create_transaction(*args, **kwargs)

    monkeypatch.setattr(transactions, 'create_transaction', ledger)
    create = subscriptions.create_paid_subscription
    creates = 0

    async def provision(*args, **kwargs):
        nonlocal creates
        creates += 1
        if failure == 'provision' and creates == 1:
            raise RuntimeError('Injected daily provisioning failure')
        return await create(*args, **kwargs)

    monkeypatch.setattr(subscriptions, 'create_paid_subscription', provision)
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            db.add(
                User(
                    id=1,
                    telegram_id=101,
                    language='ru',
                    balance_kopeks=10000,
                    promo_offer_discount_percent=promo,
                    promo_offer_discount_source='test',
                    promo_offer_discount_expires_at=now + timedelta(days=1),
                    remnawave_id=9001,
                )
            )
            db.add(
                Tariff(
                    id=1,
                    name='Daily',
                    is_active=True,
                    is_daily=True,
                    daily_price_kopeks=100,
                    device_limit=1,
                    device_price_kopeks=100,
                    traffic_limit_gb=100,
                    allowed_squads=['test-squad'],
                )
            )
            if not fresh:
                db.add(
                    Subscription(
                        id=10,
                        user_id=1,
                        tariff_id=None,
                        is_trial=False,
                        status='active',
                        device_limit=3,
                        end_date=now + timedelta(days=10),
                        remnawave_id=9001,
                        remnawave_short_id='test',
                        subscription_url='https://vpn.example/sub/test',
                    )
                )
            await db.commit()
        cart = {'tariff_id': 1, 'subscription_id': None if fresh else 10}

        async def purchase():
            async with maker() as db:
                return await auto._auto_purchase_daily_tariff(db, await users.get_user_by_id(db, 1), cart)

        if failure is None:
            first = asyncio.create_task(purchase())
            await asyncio.wait_for(entered.wait(), 10)
            second = asyncio.create_task(purchase())
            try:
                # Old code reaches the second debit before first activation; fixed
                # code blocks on the first transaction's user lock instead.
                try:
                    await asyncio.wait_for(second_debit.wait(), 0.5)
                except TimeoutError:
                    pass
            finally:
                release.set()
            results = await asyncio.gather(first, second)
            assert results == [True, False]
        elif failure == 'provision' and not fresh:
            # Existing-row provisioning has no create hook: exercise rollback at
            # the ledger boundary after its fields have been staged instead.
            monkeypatch.setattr(
                transactions, 'create_transaction', AsyncMock(side_effect=RuntimeError('Injected ledger failure'))
            )
            assert not await purchase()
            monkeypatch.setattr(transactions, 'create_transaction', create_transaction)
            assert await purchase()
        else:
            assert not await purchase()
            assert await purchase()
        async with maker() as db:
            user = await users.get_user_by_id(db, 1)
            base = 100 if fresh else 300
            price = base - base * promo // 100
            assert user.balance_kopeks == 10000 - price
            payments = (await db.scalars(select(Transaction))).all()
            assert len(payments) == 1 and payments[0].amount_kopeks == -price and payments[0].is_completed
            rows = (await db.scalars(select(Subscription))).all()
            assert len(rows) == 1 and rows[0].tariff_id == 1 and not rows[0].is_trial
            assert rows[0].last_daily_charge_at is not None and not rows[0].is_daily_paused
            assert abs((rows[0].end_date - (now + timedelta(days=1))).total_seconds()) < 20
            assert rows[0].device_limit == (1 if fresh else 3)
            assert user.promo_offer_discount_percent == 0
