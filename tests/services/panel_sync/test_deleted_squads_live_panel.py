"""Opt-in TG120 acceptance against a disposable loopback Remnawave."""

import asyncio
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

from app.config import settings
from app.external.remnawave_api import RemnaWaveAPI, RemnaWaveAPIError, UserStatus
from app.services import grace_access_runtime as grace
from app.services.panel_sync import push_subscription
from app.services.panel_sync.identity import PanelIdentity
from tests.services.panel_sync.test_writer import _sub, _user


@pytest.mark.parametrize('multi', [False, True], ids=['single', 'multi'])
@pytest.mark.parametrize('write_path', ['create', 'update', 'grace-update'])
@pytest.mark.parametrize('has_survivor', [False, True], ids=['all-deleted', 'one-survives'])
async def test_deleted_squads_against_real_panel(monkeypatch, write_path, has_survivor, multi):
    url, token_file = os.environ.get('TEST_REMNAWAVE_URL'), os.environ.get('TEST_REMNAWAVE_TOKEN_FILE')
    if not url or not token_file:
        pytest.skip('Explicit disposable loopback Remnawave URL/token required')
    assert urlparse(url).hostname in {'127.0.0.1', 'localhost', '::1'}
    token = (await asyncio.to_thread(Path(token_file).read_text)).strip()
    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_API_FALLBACK_ENABLED', False)
    now = datetime.now(UTC)
    headers = {'Authorization': f'Bearer {token}', 'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '127.0.0.1'}
    squads = []
    account_id = None
    async with httpx.AsyncClient(base_url=url, headers=headers, trust_env=False, timeout=15) as setup:
        try:
            for _ in range(2):
                response = await setup.post(
                    '/api/internal-squads', json={'name': 'test-' + secrets.token_hex(4), 'inbounds': []}
                )
                response.raise_for_status()
                squads.append(response.json()['response']['uuid'])
            if write_path != 'create':
                response = await setup.post(
                    '/api/users',
                    json={
                        'username': 'test-' + secrets.token_hex(6),
                        'expireAt': (now + timedelta(days=10)).isoformat(),
                        'activeInternalSquads': squads,
                    },
                )
                response.raise_for_status()
                account_id = response.json()['response']['id']
            response = await setup.delete('/api/internal-squads/' + squads[1])
            response.raise_for_status()
            sub = _sub(
                connected_squads=squads if has_survivor else squads[1:],
                end_date=now + timedelta(days=30),
                device_limit=0,
                remnawave_short_id=secrets.token_hex(6),
            )
            async with RemnaWaveAPI(url, token) as api:

                async def write_subscription():
                    if write_path == 'grace-update':
                        target = grace._PanelTarget(
                            status=UserStatus.ACTIVE,
                            expire_at=sub.end_date,
                            traffic_limit_bytes=10 * 1024**3,
                            squad_uuids=tuple(sub.connected_squads),
                            external_squad_uuid=None,
                            device_limit=0,
                        )
                        updated, effective_target = await grace._write_recovered_billing_target(api, account_id, target)
                        assert grace._panel_user_matches_target(updated, effective_target)
                        assert effective_target.squad_uuids == tuple(squads[:1])
                        assert target.squad_uuids == tuple(sub.connected_squads)
                        return account_id
                    result = await push_subscription(
                        api,
                        _user(),
                        sub,
                        identity=PanelIdentity(known_id=account_id),
                        multi_tariff=multi,
                        reset_devices=False,
                        now=now,
                    )
                    return result.panel_user_id

                if not has_survivor:
                    with pytest.raises(RemnaWaveAPIError):
                        await write_subscription()
                    if account_id:
                        response = await setup.get(f'/api/users/{account_id}')
                        response.raise_for_status()
                        assert [s['uuid'] for s in response.json()['response']['activeInternalSquads']] == squads[:1]
                else:
                    account_id = await write_subscription()
                    response = await setup.get(f'/api/users/{account_id}')
                    response.raise_for_status()
                    actual = response.json()['response']
                    assert [s['uuid'] for s in actual['activeInternalSquads']] == squads[:1]
                    assert actual['trafficLimitBytes'] == 10 * 1024**3
                    end = datetime.fromisoformat(actual['expireAt'].replace('Z', '+00:00'))
                    assert abs((end - sub.end_date).total_seconds()) < 1
        finally:
            if account_id:
                await setup.delete(f'/api/users/{account_id}')
            for uuid in squads:
                await setup.delete('/api/internal-squads/' + uuid)
