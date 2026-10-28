"""Explicit opt-in: real HTTP checkout, PostgreSQL, Redis and disposable Remnawave."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.cabinet.routes.subscription_modules import purchase
from app.config import settings
from app.database.crud.yookassa import create_yookassa_payment
from app.database.models import Subscription, Tariff, Transaction, TransactionType, User
from app.external.remnawave_api import RemnaWaveAPI
from app.services import subscription_auto_purchase_service as auto
from app.services.payment import common
from app.services.payment_service import PaymentService
from app.services.subscription_service import SubscriptionService
from app.services.user_cart_service import UserCartService
from app.utils.cache import CacheService
from tests.database.test_tariff_purchase_devices_postgres import checkout_context
from tests.fixtures.real_redis import ensure_real_redis


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('daily', [False, True])
@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('trial', [False, True])
@pytest.mark.parametrize('topup', [False, True])
async def test_selected_devices_reach_panel_and_survive_replay(
    postgres_database, monkeypatch, daily, multi, trial, topup
):
    url = os.environ.get('TEST_REMNAWAVE_URL')
    token_path = os.environ.get('TEST_REMNAWAVE_TOKEN_FILE')
    redis_url = os.environ.get('TEST_CART_REDIS_URL')
    if not url or not token_path or not redis_url:
        pytest.skip('Disposable loopback panel/token and isolated Redis required')
    assert urlparse(url).hostname in {'127.0.0.1', 'localhost', '::1'}
    assert urlparse(redis_url).hostname in {'127.0.0.1', 'localhost', '::1'}
    token = (await asyncio.to_thread(Path(token_path).read_text)).strip()
    create_user = SubscriptionService.create_remnawave_user
    update_user = SubscriptionService.update_remnawave_user
    redis = ensure_real_redis(monkeypatch).create_redis(redis_url)
    real_cache = CacheService()
    real_cache.redis_client, real_cache._connected = redis, True
    panel_id = None
    try:
        async with checkout_context(postgres_database, monkeypatch) as checkout:
            monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
            monkeypatch.setattr(settings, 'REMNAWAVE_API_URL', url)
            monkeypatch.setattr(settings, 'REMNAWAVE_API_KEY', token)
            monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_API_FALLBACK_ENABLED', False)
            monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', create_user)
            monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', update_user)
            monkeypatch.setattr('app.services.tariff_purchase_quote.cache', real_cache)
            cart = UserCartService()
            cart._initialized, cart._redis_client = True, redis
            monkeypatch.setattr(purchase, 'user_cart_service', cart)
            monkeypatch.setattr(auto, 'user_cart_service', cart)
            monkeypatch.setattr(common, 'user_cart_service', cart)
            monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
            monkeypatch.setattr('app.services.referral_service.process_referral_topup', AsyncMock())
            monkeypatch.setattr(common, 'notify_email_user_topup', AsyncMock())
            monkeypatch.setattr(auto, '_notify_email_user_auto_purchase', AsyncMock())
            monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
            async with RemnaWaveAPI(url, token) as api:
                squads = await api.get_internal_squads()
                assert squads
            async with checkout.maker() as db:
                tariff = await db.get(Tariff, 7)
                tariff.allowed_squads = [squads[0].uuid]
                tariff.is_daily = daily
                tariff.daily_price_kopeks = 1000
                tariff.device_price_kopeks = 3000
                if topup:
                    (await db.get(User, 1)).balance_kopeks = 0
                if trial:
                    db.add(
                        Subscription(
                            id=10,
                            user_id=1,
                            tariff_id=7,
                            is_trial=True,
                            status='active',
                            device_limit=1,
                            end_date=datetime.now(UTC) + timedelta(days=1),
                        )
                    )
                await db.commit()
                if trial:
                    trial_sub = await db.get(Subscription, 10)
                    await SubscriptionService().create_remnawave_user(db, trial_sub)
                    await db.refresh(trial_sub)
                    panel_id = trial_sub.remnawave_id
                    assert panel_id is not None
            trial_panel_id = panel_id
            parameters = {'tariff_id': 7, 'period_days': 1 if daily else 30, 'selected_device_limit': 3}
            preview = await checkout.send('/purchase-tariff/preview', parameters)
            assert preview.status_code == 200, preview.text
            expected = 1200 if daily else 36000
            assert preview.json()['total_kopeks'] == expected
            body = parameters | {'quote_id': preview.json()['quote_id'], 'checkout_id': str(uuid4())}
            response = await checkout.send('/purchase-tariff', body)
            if topup:
                assert response.status_code == 402, response.text
                saved_cart = await cart.get_user_cart(1)
                assert saved_cart is not None
                async with checkout.maker() as db:
                    await create_yookassa_payment(
                        db,
                        user_id=1,
                        yookassa_payment_id='masked-payment',
                        amount_kopeks=expected,
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
                                'amount_value': expected / 100,
                                'amount_currency': 'RUB',
                            }
                        )
                    )
                    event = {
                        'event': 'payment.succeeded',
                        'object': {
                            'id': 'masked-payment',
                            'status': 'succeeded',
                            'paid': True,
                        },
                    }
                    assert await service.process_yookassa_webhook(db, event)
                    assert await service.process_yookassa_webhook(db, event)
                    deposits = (
                        await db.scalars(
                            select(Transaction).where(
                                Transaction.type == TransactionType.DEPOSIT.value,
                            )
                        )
                    ).all()
                    assert len(deposits) == 1 and deposits[0].amount_kopeks == expected
                    paid_sub = (await db.scalars(select(Subscription))).one()
                    assert not paid_sub.is_trial and paid_sub.device_limit == 3
                    assert paid_sub.remnawave_id is not None
                    panel_id = paid_sub.remnawave_id
                    await db.refresh(await db.get(User, 1))
                    assert (await db.get(User, 1)).balance_kopeks == 0
                    charges = (
                        await db.scalars(
                            select(Transaction).where(
                                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                            )
                        )
                    ).all()
                    assert len(charges) == 1 and abs(charges[0].amount_kopeks) == expected
                response = await checkout.send('/purchase-tariff', body)
            assert response.status_code == 200, response.text
            async with checkout.maker() as db:
                sub = (await db.scalars(select(Subscription))).one()
                panel_id = sub.remnawave_id
                assert panel_id is not None
                if trial:
                    assert panel_id == trial_panel_id
                assert sub.device_limit == 3 and not sub.is_trial
                assert (await db.get(User, 1)).balance_kopeks == (0 if topup else 100000 - expected)
                paid_until = sub.end_date
            async with RemnaWaveAPI(url, token) as api:
                remote = await api._make_request('GET', f'/api/users/{panel_id}')
                assert remote['response']['hwidDeviceLimit'] == 3
                assert remote['response']['status'] == 'ACTIVE'
            assert (await checkout.send('/purchase-tariff', body)).status_code == 200
            async with checkout.maker() as db:
                sub = (await db.scalars(select(Subscription))).one()
                assert sub.end_date == paid_until
                assert (await db.get(User, 1)).balance_kopeks == (0 if topup else 100000 - expected)
                # Run the real update path too, not only first create.
                assert await SubscriptionService().sync_remnawave_user(db, sub, reset_traffic=False)
            async with RemnaWaveAPI(url, token) as api:
                remote = await api._make_request('GET', f'/api/users/{panel_id}')
                assert remote['response']['hwidDeviceLimit'] == 3
    finally:
        if panel_id is not None:
            async with RemnaWaveAPI(url, token) as api:
                await api.delete_user(panel_id)
        await redis.delete('user_cart:1', 'user_cart:1:sub:10', 'cart_topup_intent:1', 'cart_topup_intent:1:sub:10')
        await redis.aclose()
