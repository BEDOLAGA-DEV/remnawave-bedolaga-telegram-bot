"""Opt-in timeout/restart acceptance against a disposable loopback Remnawave."""

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.database.models import Base, PaymentMethod, Subscription, Transaction, User
from app.external.remnawave_api import RemnaWaveAPI
from app.services import remnawave_retry_queue as retry
from app.services.remnawave_webhook_service import RemnaWaveWebhookService
from app.services.subscription_renewal_service import SubscriptionRenewalService
from app.services.subscription_service import SubscriptionService
from tests.database.test_renewal_survives_stale_panel_snapshot_postgres import BALANCE, PRICE, _pricing, _seed
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('timeout_after_write', [False, True], ids=['before-write', 'lost-response'])
@pytest.mark.parametrize('multi', [False, True], ids=['single', 'multi'])
async def test_paid_expiry_recovers_against_real_panel(postgres_database, monkeypatch, timeout_after_write, multi):
    url, token_file = os.environ.get('TEST_REMNAWAVE_URL'), os.environ.get('TEST_REMNAWAVE_TOKEN_FILE')
    if not url or not token_file:
        pytest.skip('Explicit disposable loopback Remnawave URL/token required')
    assert urlparse(url).hostname in {'127.0.0.1', 'localhost', '::1'}
    token = (await asyncio.to_thread(Path(token_file).read_text)).strip()
    for key, value in {
        'SALES_MODE': 'tariffs',
        'MULTI_TARIFF_ENABLED': multi,
        'ADMIN_NOTIFICATIONS_ENABLED': False,
        'RESET_TRAFFIC_ON_PAYMENT': False,
        'RESET_DEVICES_ON_RENEWAL': False,
        'HAPP_CRYPTOLINK_API_FALLBACK_ENABLED': False,
        'REMNAWAVE_API_URL': url,
        'REMNAWAVE_API_KEY': token,
    }.items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(retry, 'remnawave_retry_queue', retry.RemnaWaveRetryQueue())
    headers = {'Authorization': f'Bearer {token}', 'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '127.0.0.1'}
    fault_enabled = True
    attempted = False
    original_request = RemnaWaveAPI._make_request

    async def request_with_timeout(self, method, endpoint, *args, **kwargs):
        nonlocal attempted
        if fault_enabled and method == 'PATCH' and endpoint == '/api/users':
            attempted = True
            if timeout_after_write:
                await original_request(self, method, endpoint, *args, **kwargs)
            raise TimeoutError('Synthetic transport timeout against disposable panel')
        return await original_request(self, method, endpoint, *args, **kwargs)

    monkeypatch.setattr(RemnaWaveAPI, '_make_request', request_with_timeout)

    @asynccontextmanager
    async def client(self):
        async with RemnaWaveAPI(base_url=url, api_key=token) as api:
            yield api

    monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
    async with httpx.AsyncClient(base_url=url, headers=headers, timeout=15, trust_env=False) as setup:
        old = datetime.now(UTC) - timedelta(days=1)
        response = await setup.post(
            '/api/users',
            json={
                'username': 'w3-' + secrets.token_hex(6),
                'telegramId': 1,
                'expireAt': old.isoformat(),
                'hwidDeviceLimit': 3,
            },
        )
        assert response.is_success, response.status_code
        account = response.json()['response']
        try:
            async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
                user, subscription, _ = await _seed(db, datetime.now(UTC))
                user.remnawave_id = subscription.remnawave_id = account['id']
                subscription.remnawave_short_id = account['shortUuid']
                subscription.end_date = old
                subscription.connected_squads = []
                await db.refresh(subscription, ['tariff'])
                subscription.tariff.allowed_squads = []
                await db.commit()
                result = await SubscriptionRenewalService().finalize(
                    db,
                    user,
                    subscription,
                    _pricing(),
                    description='Local recovery acceptance',
                    payment_method=PaymentMethod.BALANCE,
                )
                assert result is not None and attempted
                await db.refresh(subscription)
                target = subscription.end_date
                assert subscription.pending_panel_expire_at == target
                panel_response = await setup.get(f'/api/users/{account["id"]}')
                actual = datetime.fromisoformat(panel_response.json()['response']['expireAt'].replace('Z', '+00:00'))
                assert abs((actual - (target if timeout_after_write else old)).total_seconds()) < 1
                await db.execute(update(Transaction).values(created_at=datetime.now(UTC) - timedelta(hours=73)))
                await db.commit()
                user_id, subscription_id = user.id, subscription.id
                db.expunge_all()  # discard ORM objects as well as all memory retries
                queue = retry.RemnaWaveRetryQueue(max_retries=1)
                monkeypatch.setattr(retry, 'remnawave_retry_queue', queue)
                user = await db.get(User, user_id)
                subscription = await db.get(Subscription, subscription_id)
                service = RemnaWaveWebhookService(AsyncMock())
                monkeypatch.setattr(service, '_notify_user', AsyncMock())
                for event, handler in [
                    ('user.modified', service._handle_user_modified),
                    ('user.expired', service._handle_user_expired),
                ]:
                    await service._process_user_event(db, event, dict(account, status='EXPIRED'), handler)
                await db.refresh(subscription)
                assert subscription.status == 'active' and subscription.end_date == target
                await db.commit()
                # Lose the webhook's in-memory enqueue too: only DB intent remains.
                queue = retry.RemnaWaveRetryQueue(max_retries=1)
                monkeypatch.setattr(retry, 'remnawave_retry_queue', queue)
                monkeypatch.setattr(retry, 'AsyncSessionLocal', async_sessionmaker(db.bind, expire_on_commit=False))
                fault_enabled = False
                await queue.process_pending()
                await db.refresh(subscription)
                await db.refresh(user)
                assert subscription.pending_panel_expire_at is None
                panel_response = await setup.get(f'/api/users/{account["id"]}')
                current = panel_response.json()['response']
                assert current['status'] == 'ACTIVE'
                assert (
                    abs((datetime.fromisoformat(current['expireAt'].replace('Z', '+00:00')) - target).total_seconds())
                    < 1
                )
                assert user.balance_kopeks == BALANCE - PRICE
                assert (
                    len(list(await db.scalars(select(Transaction).where(Transaction.type == 'subscription_payment'))))
                    == 1
                )
                assert subscription.remnawave_id == account['id']
        finally:
            deleted = await setup.delete(f'/api/users/{account["id"]}')
            assert deleted.is_success, deleted.status_code
