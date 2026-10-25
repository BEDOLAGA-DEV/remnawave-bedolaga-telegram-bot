"""Opt-in real-panel acknowledgement of durable paid daily reset delivery."""

import asyncio
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

from app.config import settings
from app.database.models import Base, PaidTrafficResetIntent, Subscription, Transaction, User
from app.external.remnawave_api import RemnaWaveAPI
from app.services import paid_traffic_reset_service as resets
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('lose_response', [False, True])
async def test_real_panel_reset_ack_and_lost_response(postgres_database, monkeypatch, multi, lose_response):
    url = os.environ.get('TEST_REMNAWAVE_URL')
    token_file = os.environ.get('TEST_REMNAWAVE_TOKEN_FILE')
    if not url or not token_file:
        pytest.skip('Explicit disposable loopback Remnawave URL/token required')
    assert urlparse(url).hostname in {'127.0.0.1', 'localhost', '::1'}
    token = (await asyncio.to_thread(Path(token_file).read_text)).strip()
    monkeypatch.setattr(settings, 'REMNAWAVE_API_URL', url)
    monkeypatch.setattr(settings, 'REMNAWAVE_API_KEY', token)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_API_FALLBACK_ENABLED', False)
    now = datetime.now(UTC)
    reset_calls = 0
    original_reset = RemnaWaveAPI.reset_user_traffic

    async def counted_reset(api, panel_id, **kwargs):
        nonlocal reset_calls
        reset_calls += 1
        acknowledged = await original_reset(api, panel_id, **kwargs)
        if lose_response:
            raise TimeoutError('Synthetic loss after the real panel acknowledged the reset')
        return acknowledged

    monkeypatch.setattr(RemnaWaveAPI, 'reset_user_traffic', counted_reset)
    headers = {'Authorization': f'Bearer {token}', 'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '127.0.0.1'}
    async with httpx.AsyncClient(base_url=url, headers=headers, trust_env=False, timeout=15) as setup:
        response = await setup.post(
            '/api/users',
            json={
                'username': 'test-' + secrets.token_hex(6),
                'expireAt': (now + timedelta(days=2)).isoformat(),
                'trafficLimitBytes': 10 * 1024**3,
            },
        )
        response.raise_for_status()
        account = response.json()['response']
        try:
            async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
                db.add(User(id=1, status='active', balance_kopeks=99900, remnawave_id=account['id']))
                await db.flush()
                sub = Subscription(
                    id=1,
                    user_id=1,
                    status='active',
                    is_trial=False,
                    start_date=now,
                    end_date=now + timedelta(days=2),
                    last_daily_charge_at=now,
                    traffic_limit_gb=10,
                    traffic_used_gb=3,
                    remnawave_id=account['id'],
                    remnawave_short_id=account['shortUuid'],
                    remnawave_short_uuid=account['shortUuid'],
                )
                transaction = Transaction(
                    user_id=1, type='subscription_payment', amount_kopeks=-100, description='Synthetic daily charge'
                )
                db.add_all([sub, transaction])
                await db.flush()
                intent_id = await resets.stage_daily_reset(db, sub, transaction, reason='synthetic daily charge')
                await db.commit()
                if lose_response:
                    with pytest.raises(TimeoutError):
                        await resets.deliver_daily_reset(db, intent_id)
                    # A new reconciliation reads the panel marker; it cannot resend POST.
                    assert await resets.deliver_daily_reset(db, intent_id)
                else:
                    assert await resets.deliver_daily_reset(db, intent_id)
                assert await resets.deliver_daily_reset(db, intent_id)
                intent = await db.get(PaidTrafficResetIntent, intent_id)
                assert intent.state == 'confirmed'
                assert intent.confirmed_last_reset_at is not None
                assert not intent.activation_pending
                assert reset_calls == 1
                await db.refresh(sub)
                assert sub.traffic_used_gb == 0
                assert (await db.get(User, 1)).balance_kopeks == 99900
        finally:
            await setup.delete(f'/api/users/{account["id"]}')
