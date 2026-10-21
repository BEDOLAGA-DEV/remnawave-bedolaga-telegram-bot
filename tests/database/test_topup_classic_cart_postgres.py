"""Real provider finalizers → wallet hook → cart → purchase on PostgreSQL.

Only key/value storage, notifications and panel transport are fake. Payment
credit, common dispatch, pricing, purchase persistence and ledgers are real.
"""

import fnmatch
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib import import_module
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import (
    Base,
    CasheraPayment,
    LavaPayment,
    PlategaPayment,
    Subscription,
    Tariff,
    Transaction,
    User,
)
from app.services import subscription_auto_purchase_service as auto
from app.services.payment import common
from app.services.subscription_service import SubscriptionService
from app.services.user_cart_service import UserCartService
from tests.database.test_classic_device_migration_postgres import Panel
from tests.fixtures.postgres_db import postgres_session
from tests.fixtures.real_redis import ensure_real_redis


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


class CartStorage:
    def __init__(self):
        self.values = {}

    async def setex(self, key, ttl, value):
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def exists(self, key):
        return int(key in self.values)

    async def delete(self, *keys):
        return sum(self.values.pop(key, None) is not None for key in keys)

    async def scan(self, cursor=0, match='*', count=50):
        return 0, [key for key in self.values if fnmatch.fnmatch(key, match)]


@pytest_asyncio.fixture
async def cart_service(monkeypatch):
    service = UserCartService()
    url = os.environ.get('TEST_CART_REDIS_URL')
    service._redis_client = ensure_real_redis(monkeypatch).create_redis(url) if url else CartStorage()
    service._initialized = True
    if not url:
        # Sequential storage boundary for the standalone PostgreSQL profile;
        # the combined Redis/PostgreSQL profile executes the real Lua script.
        async def cleanup(user_id, processed):
            await service.delete_subscription_cart(user_id, processed['subscription_id'])
            return await service.delete_user_cart(user_id)

        service.delete_processed_cart = AsyncMock(side_effect=cleanup)
    try:
        yield service
    finally:
        if url:
            await service._redis_client.delete(
                'user_cart:1', 'user_cart:1:sub:10', 'user_cart:1:sub:20', 'cart_topup_intent:1'
            )
            await service._redis_client.aclose()


@pytest.mark.parametrize('provider', ['lava', 'cashera', 'platega'])
@pytest.mark.parametrize('cart_case', ['global', 'per_subscription', 'daily_neighbor', 'stale_intent'])
async def test_topup_finalizer_purchases_selected_classic(
    postgres_database, monkeypatch, provider, cart_case, cart_service
):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'DAILY_SUBSCRIPTIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 10)
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
    service = cart_service
    monkeypatch.setattr(common, 'user_cart_service', service)
    monkeypatch.setattr(auto, 'user_cart_service', service)
    now = datetime.now(UTC)
    panel = Panel(now)

    @asynccontextmanager
    async def client(self):
        yield panel

    monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(User(id=1, telegram_id=101, language='ru', status='active', balance_kopeks=0, remnawave_id=9002))
        db.add_all(
            [
                Tariff(
                    id=1,
                    name='Target',
                    is_active=True,
                    device_limit=1,
                    device_price_kopeks=100,
                    traffic_limit_gb=100,
                    period_prices={'30': 300},
                    allowed_squads=['test-squad'],
                ),
                Tariff(
                    id=2,
                    name='Neighbor',
                    is_active=True,
                    device_limit=1,
                    is_daily=cart_case == 'daily_neighbor',
                    daily_price_kopeks=100,
                ),
            ]
        )
        for sub_id, tariff_id, panel_id, cap in [(10, None, 9001, 3), (20, 2, 9002, 1)]:
            db.add(
                Subscription(
                    id=sub_id,
                    user_id=1,
                    tariff_id=tariff_id,
                    remnawave_id=panel_id,
                    remnawave_short_id=f'test-{sub_id}',
                    subscription_url='https://vpn.example/sub/test',
                    device_limit=cap,
                    is_trial=False,
                    autopay_enabled=True,
                    status='disabled' if sub_id == 20 and cart_case == 'daily_neighbor' else 'active',
                    end_date=now + timedelta(days=-1 if sub_id == 20 and cart_case == 'daily_neighbor' else 10),
                    updated_at=now - timedelta(minutes=20),
                )
            )
        model = {'lava': LavaPayment, 'cashera': CasheraPayment, 'platega': PlategaPayment}[provider]
        payment = model(
            user_id=1,
            amount_kopeks=1000,
            currency='RUB',
            status='pending',
            is_paid=False,
            **(
                {'correlation_id': 'test-platega', 'payment_method_code': 2}
                if provider == 'platega'
                else {'order_id': f'test-{provider}'}
            ),
        )
        db.add(payment)
        await db.commit()
        user = await get_user_by_id(db, 1)
        neighbor = await db.get(Subscription, 20)
        before = (neighbor.status, neighbor.end_date, neighbor.device_limit)
        assert await service.save_user_cart(
            1,
            {
                'cart_mode': 'tariff_purchase',
                'tariff_id': 1,
                'period_days': 30,
                'subscription_id': 10,
                'total_price': 500,
                'return_to_cart': True,
            },
        )
        if cart_case == 'per_subscription':
            await service.delete_global_cart_only(1)
        elif cart_case == 'stale_intent':
            await service.clear_topup_intent(1)
        module = import_module(f'app.services.payment.{provider}')
        actor = getattr(module, provider.capitalize() + 'PaymentMixin')()
        actor.bot = None

        async def finalize():
            method = getattr(actor, f'_finalize_{provider}_payment')
            if provider == 'platega':
                return await method(db, payment, payload={})
            return await method(db, payment, trigger='test-confirmed-callback')

        assert await finalize()
        await db.refresh(user)
        selected = await db.get(Subscription, 10)
        if cart_case == 'stale_intent':
            assert selected.tariff_id is None and selected.device_limit == 3
            assert user.balance_kopeks == 1000
            assert [t.amount_kopeks for t in (await db.scalars(select(Transaction))).all()] == [1000]
            assert await service.get_user_cart(1)
            assert panel.patches == []
            assert await finalize()
            await db.refresh(user)
            assert user.balance_kopeks == 1000
            return
        assert selected.tariff_id == 1 and selected.device_limit == 3
        assert selected.remnawave_id == 9001
        assert user.balance_kopeks == 500
        await db.refresh(neighbor)
        assert (neighbor.status, neighbor.end_date, neighbor.device_limit) == before
        rows = (await db.scalars(select(Transaction))).all()
        assert sorted(row.amount_kopeks for row in rows) == [-500, 1000]
        assert await service.get_user_cart(1) is None
        assert await service.get_all_subscription_carts(1) == []
        assert len(panel.patches) == 1 and panel.patches[0]['hwidDeviceLimit'] == 3
        assert await finalize()
        await db.refresh(user)
        assert user.balance_kopeks == 500
        assert len((await db.scalars(select(Transaction))).all()) == 2
