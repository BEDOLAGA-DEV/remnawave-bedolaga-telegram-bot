"""Opt-in real panel API checks; counters are fixtures, no VPN node is required."""

import asyncio
import os
import secrets
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.database.models import Base, Subscription, TrafficPurchase, User
from app.external.remnawave_api import RemnaWaveAPI
from app.services.traffic_accounting_delivery import deliver_account_projection
from app.services.traffic_accounting_service import ensure_managed_account, lock_accounting_subscription
from app.services.traffic_accounting_store import load_account
from app.services.traffic_purchase_service import add_subscription_traffic
from tests.fixtures.postgres_db import postgres_engine


pytestmark = pytest.mark.postgres
GB = 1024**3


async def _set_usage(panel_id: int, used_gb: int):
    # Deliberately restricted to the disposable lab created for this test.
    # Never use production credentials or a production database here.
    container = os.environ.get('TEST_REMNAWAVE_COUNTER_CONTAINER')
    assert container == 'bedolaga-traffic-panel-lab-remnawave-db-1'
    assert type(panel_id) is int and panel_id > 0
    assert type(used_gb) is int and used_gb >= 0
    sql = (
        f'UPDATE user_traffic SET used_traffic_bytes={used_gb * GB}, '
        f'lifetime_used_traffic_bytes={used_gb * GB} WHERE id={panel_id};'
    )
    await asyncio.to_thread(
        subprocess.run,
        ['docker', 'exec', container, 'psql', '-U', 'postgres', '-d', 'postgres', '-v', 'ON_ERROR_STOP=1', '-c', sql],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('lose_response', [False, True])
async def test_real_panel_exhausted_topup_expiry_and_calendar(postgres_database, monkeypatch, multi, lose_response):
    url = os.environ.get('TEST_REMNAWAVE_URL')
    token_file = os.environ.get('TEST_REMNAWAVE_TOKEN_FILE')
    if not url or not token_file or not os.environ.get('TEST_REMNAWAVE_COUNTER_CONTAINER'):
        pytest.skip('Explicit disposable loopback panel and counter-fixture container required')
    assert urlparse(url).hostname in {'127.0.0.1', 'localhost', '::1'}
    token = (await asyncio.to_thread(Path(token_file).read_text)).strip()
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_API_FALLBACK_ENABLED', False)
    monkeypatch.setattr('app.services.traffic_accounting_service.schedule_projection_after_commit', lambda *_: None)
    now = datetime.now(UTC)
    headers = {'Authorization': f'Bearer {token}', 'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '127.0.0.1'}
    async with httpx.AsyncClient(base_url=url, headers=headers, trust_env=False, timeout=20) as setup:
        response = await setup.post(
            '/api/users',
            json={
                'username': 'ledger-' + secrets.token_hex(6),
                'expireAt': (now + timedelta(days=60)).isoformat(),
                'trafficLimitBytes': 80 * GB,
                'trafficLimitStrategy': 'MONTH',
            },
        )
        response.raise_for_status()
        panel_id = response.json()['response']['id']
        try:
            await _set_usage(panel_id, 80)
            async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
                factory = async_sessionmaker(engine, expire_on_commit=False)
                async with factory() as db:
                    db.add(User(id=1, status='active', remnawave_id=panel_id))
                    db.add(
                        Subscription(
                            id=1,
                            user_id=1,
                            status='active',
                            is_trial=False,
                            start_date=now,
                            end_date=now + timedelta(days=60),
                            remnawave_id=panel_id,
                            traffic_limit_gb=80,
                            purchased_traffic_gb=40,
                        )
                    )
                    db.add(TrafficPurchase(subscription_id=1, traffic_gb=40, expires_at=now + timedelta(hours=1)))
                    await db.commit()
                async with RemnaWaveAPI(url, token) as api:
                    await ensure_managed_account(factory, api, 1, now=now, timezone='UTC')
                    await deliver_account_projection(factory, api, 1, now=now)
                    exhausted = await api.get_user_by_id(panel_id)
                    assert exhausted.status.value == 'DISABLED'
                    assert exhausted.traffic_limit_strategy.value == 'NO_RESET'
                    assert exhausted.traffic_limit_bytes > 0  # zero means unlimited
                    async with factory() as db:
                        sub, _ = await lock_accounting_subscription(db, 1)
                        observed = await api.get_user_by_id(panel_id)
                        await add_subscription_traffic(db, sub, 40, panel=observed, commit=False)
                        await db.commit()
                    if lose_response:
                        original_update = api.update_user
                        dropped = False

                        async def lost_update_response(*args, **kwargs):
                            nonlocal dropped
                            result = await original_update(*args, **kwargs)
                            if not dropped and kwargs.get('traffic_limit_bytes') is not None:
                                dropped = True
                                raise TimeoutError('Lost response after real panel applied the quota')
                            return result

                        monkeypatch.setattr(api, 'update_user', lost_update_response)
                        with pytest.raises(TimeoutError):
                            await deliver_account_projection(factory, api, 1, now=now)
                        assert dropped
                    await deliver_account_projection(factory, api, 1, now=now)
                    bought = await api.get_user_by_id(panel_id)
                    assert bought.status.value == 'ACTIVE'
                    assert bought.traffic_limit_bytes - bought.used_traffic_bytes == 40 * GB
                    await _set_usage(panel_id, 90)
                    await deliver_account_projection(factory, api, 1, now=now + timedelta(hours=2))
                    async with factory() as db:
                        account, state = await load_account(db, 1)
                        assert account.remaining_bytes(now + timedelta(hours=2)) == 30 * GB
                        next_grant = state.next_refresh_at + timedelta(seconds=1)
                    await deliver_account_projection(factory, api, 1, now=next_grant)
                    await deliver_account_projection(factory, api, 1, now=next_grant)
                    renewed = await api.get_user_by_id(panel_id)
                    assert renewed.traffic_limit_strategy.value == 'NO_RESET'
                    assert renewed.traffic_limit_bytes - renewed.used_traffic_bytes == 70 * GB
                    async with factory() as db:
                        account, _ = await load_account(db, 1)
                        assert account.remaining_bytes(next_grant) == 70 * GB
                        assert next(a for a in account.allowances if a.key == 'purchase:1').remaining_bytes == 0
        finally:
            await setup.delete(f'/api/users/{panel_id}')
