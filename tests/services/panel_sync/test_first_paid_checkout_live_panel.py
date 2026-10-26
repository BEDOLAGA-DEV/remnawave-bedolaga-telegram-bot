"""Opt-in paid checkout through real Redis/PostgreSQL/Remnawave, with a synthetic provider ACK."""

import asyncio
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.crud.yookassa import create_yookassa_payment
from app.database.models import Base, Subscription, Tariff, User
from app.external.remnawave_api import RemnaWaveAPI
from app.services import subscription_auto_purchase_service as auto
from app.services.payment import common
from app.services.payment_service import PaymentService
from app.services.remnawave_webhook_service import RemnaWaveWebhookService
from app.services.subscription_service import SubscriptionService
from tests.fixtures.postgres_db import postgres_session
from tests.services.test_cart_cleanup_redis import carts  # noqa: F401 - shared pytest fixture


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest.mark.parametrize('seed_trial', [False, True])
@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('telegram', [False, True])
async def test_first_paid_without_trial(postgres_database, monkeypatch, multi, telegram, seed_trial, carts):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    url = os.environ.get('TEST_REMNAWAVE_URL')
    token_file = os.environ.get('TEST_REMNAWAVE_TOKEN_FILE')
    if not url or not token_file:
        pytest.skip('Explicit disposable loopback Remnawave URL/token required')
    assert urlparse(url).hostname in {'localhost', '127.0.0.1', '::1'}
    token = (await asyncio.to_thread(Path(token_file).read_text)).strip()
    monkeypatch.setattr(settings, 'REMNAWAVE_API_URL', url)
    monkeypatch.setattr(settings, 'REMNAWAVE_API_KEY', token)
    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_API_FALLBACK_ENABLED', False)
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr('app.services.referral_service.process_referral_topup', AsyncMock())
    monkeypatch.setattr('app.services.payment.common.notify_email_user_topup', AsyncMock())
    monkeypatch.setattr(auto, '_notify_email_user_auto_purchase', AsyncMock())
    cart_service, user_id, _ = carts
    monkeypatch.setattr(common, 'user_cart_service', cart_service)
    synthetic_id = 100000000 + secrets.randbelow(100000000)
    panel_ids = []
    try:
        async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
            db.add(
                User(
                    id=user_id,
                    telegram_id=synthetic_id if telegram else None,
                    email=None if telegram else f'masked-{synthetic_id}@example.com',
                    status='active',
                    balance_kopeks=0,
                    language='ru',
                )
            )
            db.add(
                Tariff(
                    id=1,
                    name='Test',
                    is_active=True,
                    period_prices={'30': 14900},
                    traffic_limit_gb=100,
                    device_limit=2,
                    allowed_squads=[],
                )
            )
            await db.commit()
            if seed_trial:
                db.add(
                    Subscription(
                        user_id=user_id,
                        tariff_id=1,
                        status='pending',
                        is_trial=True,
                        end_date=datetime.now(UTC) + timedelta(days=3),
                        device_limit=2,
                        traffic_limit_gb=10,
                    )
                )
                await db.commit()
            user = await get_user_by_id(db, user_id)
            await cart_service.save_user_cart(
                user_id,
                {
                    'cart_mode': 'tariff_purchase',
                    'tariff_id': 1,
                    'period_days': 30,
                    'total_price': 14900,
                    'return_to_cart': True,
                },
            )
            await create_yookassa_payment(
                db,
                user_id=user_id,
                yookassa_payment_id='masked-payment',
                amount_kopeks=14900,
                currency='RUB',
                description='Test',
                status='pending',
            )
            service = PaymentService.__new__(PaymentService)
            service.bot = None
            service.nalogo_service = None
            service.yookassa_service = SimpleNamespace(
                get_payment_info=AsyncMock(
                    return_value={
                        'id': 'masked-payment',
                        'status': 'succeeded',
                        'paid': True,
                        'amount_value': 149,
                        'amount_currency': 'RUB',
                    }
                )
            )
            event = {
                'event': 'payment.succeeded',
                'object': {'id': 'masked-payment', 'status': 'succeeded', 'paid': True},
            }
            assert await service.process_yookassa_webhook(db, event)
            assert await service.process_yookassa_webhook(db, event)
            rows = list((await db.scalars(select(Subscription).where(Subscription.is_trial.is_(False)))).all())
            panel_ids = [row.remnawave_id for row in rows if row.remnawave_id is not None]
            assert len(rows) == 1
            assert rows[0].status == 'active' and not rows[0].is_trial
            assert rows[0].remnawave_id is not None
            async with RemnaWaveAPI(url, token) as api:
                account = await api.get_user_by_id(rows[0].remnawave_id)
                assert account.status.value == 'ACTIVE'
                raw = await api._make_request('GET', f'/api/users/{account.id}')
                webhook = RemnaWaveWebhookService(bot=None)
                assert await webhook.process_event(db, 'user.created', raw['response'])
                assert await webhook.process_event(db, 'user.modified', raw['response'])
                await db.refresh(rows[0])
                assert rows[0].status == 'active'
                assert await SubscriptionService().sync_remnawave_user(db, rows[0], reset_traffic=False)
                assert (await api.get_user_by_id(account.id)).status.value == 'ACTIVE'

            await db.refresh(user)
            assert user.balance_kopeks == 0

    finally:
        if panel_ids:
            async with RemnaWaveAPI(url, token) as api:
                for panel_id in panel_ids:
                    await api.delete_user(panel_id)
