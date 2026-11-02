"""Real Anore callback → durable deposit → common cart → purchase on PostgreSQL.

Only storage, panel transport and notifications/referral effects are faked.
The payment follow-up and saved-cart purchase execute their real implementations.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import AnorePayment, Base, Subscription, Tariff, Transaction, User
from app.services import subscription_auto_purchase_service as auto
from app.services.payment import common
from app.services.payment.anore import AnorePaymentMixin
from app.services.subscription_service import SubscriptionService
from tests.database.test_classic_device_migration_postgres import Panel
from tests.database.test_topup_classic_cart_postgres import cart_service  # noqa: F401
from tests.fixtures.postgres_db import postgres_session


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest.mark.parametrize('source_tariff', [None, 1])
@pytest.mark.parametrize('cart_case', ['global', 'per_subscription', 'stale_intent', 'unconfirmed'])
async def test_anore_callback_purchases_only_confirmed_selected_cart(
    postgres_database, monkeypatch, source_tariff, cart_case, cart_service
):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'DAILY_SUBSCRIPTIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 10)
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())
    monkeypatch.setattr('app.services.referral_service.process_referral_topup', AsyncMock())
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
                    is_daily=False,
                    daily_price_kopeks=100,
                ),
            ]
        )
        for sub_id, tariff_id, panel_id, cap in [(10, source_tariff, 9001, 3), (20, 2, 9002, 1)]:
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
                    status='active',
                    end_date=now + timedelta(days=10),
                    updated_at=now - timedelta(minutes=20),
                )
            )
        payment = AnorePayment(
            user_id=1,
            amount_kopeks=1000,
            currency='RUB',
            status='new',
            is_paid=False,
            order_id='masked-order',
            anore_payment_id='masked-invoice',
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
        elif cart_case == 'unconfirmed':
            replacement = await service.get_user_cart(1)
            replacement['return_to_cart'] = False
            assert await service.save_user_cart(1, replacement)
        actor = AnorePaymentMixin()
        actor.bot = None
        actor._send_payment_success_notification = AsyncMock()

        async def finalize():
            return await actor.process_anore_callback(
                db,
                {
                    'event': 'payment.succeeded',
                    'id': 'masked-invoice',
                    'orderId': 'masked-order',
                    'amount': 10,
                    'rubAmount': 10,
                    'currency': 'RUB',
                    'status': 'paid',
                    '_delivery_id': 'masked-delivery',
                },
            )

        assert await finalize()
        await db.refresh(user)
        selected = await db.get(Subscription, 10)
        if cart_case in ('stale_intent', 'unconfirmed'):
            assert selected.tariff_id == source_tariff and selected.device_limit == 3
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
