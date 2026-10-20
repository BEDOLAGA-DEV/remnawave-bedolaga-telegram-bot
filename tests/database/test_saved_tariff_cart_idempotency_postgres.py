"""A saved checkout must debit at most once across sessions and commits."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.database.crud import subscription as subscriptions
from app.database.crud.user import get_user_by_id
from app.database.models import Base, Subscription, Tariff, Transaction, TransactionType, User
from app.services import subscription_auto_purchase_service as auto
from app.services.subscription_service import SubscriptionService
from tests.database.test_classic_device_migration_postgres import Panel
from tests.fixtures.postgres_db import postgres_engine


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('concurrent', [False, True])
async def test_saved_cart_only_charged_once(postgres_database, monkeypatch, concurrent, failure=None, promo=0):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 10)
    monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
    monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    now = datetime.now(UTC)
    panel = Panel(now)

    @asynccontextmanager
    async def client(self):
        yield panel

    monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
    entered = asyncio.Event()
    release = asyncio.Event()
    if failure == 'ledger':
        from app.database.crud import transaction as transactions

        original_transaction = transactions.create_transaction
        ledger_calls = 0

        async def create_transaction(*args, **kwargs):
            nonlocal ledger_calls
            ledger_calls += 1
            if ledger_calls == 1:
                raise RuntimeError('Injected ledger failure')
            return await original_transaction(*args, **kwargs)

        monkeypatch.setattr(transactions, 'create_transaction', create_transaction)
    if failure == 'refund':
        monkeypatch.setattr('app.database.crud.user.add_user_balance', AsyncMock(return_value=False))
    original_extend = subscriptions.extend_subscription
    calls = 0

    async def extend(*args, **kwargs):
        nonlocal calls
        calls += 1
        if failure in ('provision', 'refund') and calls == 1:
            raise RuntimeError('Injected provisioning failure')
        if concurrent and calls == 1:
            entered.set()
            await asyncio.wait_for(release.wait(), 10)
        return await original_extend(*args, **kwargs)

    monkeypatch.setattr(subscriptions, 'extend_subscription', extend)
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            db.add(
                User(
                    id=1,
                    telegram_id=101,
                    language='ru',
                    status='active',
                    balance_kopeks=10000,
                    remnawave_id=9001,
                    promo_offer_discount_percent=promo,
                    promo_offer_discount_source='test',
                    promo_offer_discount_expires_at=now + timedelta(days=1),
                )
            )
            db.add(
                Tariff(
                    id=1,
                    name='Test',
                    is_active=True,
                    device_limit=1,
                    device_price_kopeks=100,
                    traffic_limit_gb=100,
                    period_prices={'30': 300},
                    allowed_squads=['test-squad'],
                )
            )
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
        cart = {
            'cart_mode': 'tariff_purchase',
            'tariff_id': 1,
            'period_days': 30,
            'subscription_id': 10,
            'tariff_checkout_id': 'test-checkout-1',
        }

        async def purchase():
            async with maker() as db:
                user = await get_user_by_id(db, 1)
                return await auto._auto_purchase_tariff(db, user, dict(cart))

        if concurrent:
            first = asyncio.create_task(purchase())
            try:
                await asyncio.wait_for(entered.wait(), 10)
                second = await asyncio.wait_for(purchase(), 10)
            finally:
                release.set()
            results = [await first, second]
        else:
            first_result = await purchase()
            if failure == 'new_checkout':
                cart['tariff_checkout_id'] = 'test-checkout-2'
            results = [first_result, await purchase()]
        expected_results = (
            [False, False]
            if failure == 'refund'
            else [False, True]
            if failure in ('ledger', 'provision')
            else [True, True]
            if failure == 'new_checkout'
            else [True, False]
        )
        assert results == expected_results
        async with maker() as db:
            user = await get_user_by_id(db, 1)
            sub = await db.get(Subscription, 10)
            payments = (await db.scalars(select(Transaction))).all()
            price = 500 - 500 * promo // 100
            count = 2 if failure == 'new_checkout' else 1
            total = price + 500 if failure == 'new_checkout' else price
            assert user.balance_kopeks == 10000 - total
            assert sorted(p.amount_kopeks for p in payments) == (
                [-price, -price, price]
                if failure == 'provision'
                else [-500, -price]
                if failure == 'new_checkout'
                else [-price]
            )
            if failure == 'refund':
                assert not payments[0].is_completed and payments[0].external_id
            else:
                assert (
                    sum(p.is_completed and p.type == TransactionType.SUBSCRIPTION_PAYMENT.value for p in payments)
                    == count
                )
            assert sub.end_date == now + timedelta(days=10 if failure == 'refund' else 10 + 30 * count)
            if failure == 'provision' and promo:
                assert user.promo_offer_discount_percent == 0
            assert sub.remnawave_id == 9001 and sub.device_limit == 3
            assert len(panel.patches) == (0 if failure == 'refund' else count)


@pytest.mark.parametrize('failure', ['ledger', 'provision', 'refund', 'new_checkout'])
@pytest.mark.parametrize('promo', [0, 17, 100])
async def test_saved_cart_retries_only_after_atomic_compensation(postgres_database, monkeypatch, failure, promo):
    await test_saved_cart_only_charged_once(postgres_database, monkeypatch, False, failure=failure, promo=promo)


@pytest.mark.parametrize('promo', [17, 100])
async def test_concurrent_saved_cart_consumes_promo_once(postgres_database, monkeypatch, promo):
    await test_saved_cart_only_charged_once(postgres_database, monkeypatch, True, promo=promo)
