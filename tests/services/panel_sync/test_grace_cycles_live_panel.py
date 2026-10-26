"""Opt-in repeated grace/payment transitions against a disposable loopback panel."""

import asyncio
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import pytest

from app.config import settings
from app.external.remnawave_api import RemnaWaveAPI
from app.services.grace_access_runtime import RemnawaveGracePanelGateway
from app.services.grace_access_service import (
    GraceAccessPolicy,
    GraceBillingState,
    GraceReason,
    build_panel_overlay,
)


@pytest.mark.parametrize('external_mode', ['detach', 'keep', 'replace'])
@pytest.mark.parametrize('reason', [GraceReason.EXPIRED, GraceReason.LIMITED])
@pytest.mark.parametrize('reset_traffic', [False, True])
async def test_repeated_grace_payment_preserves_squads(monkeypatch, external_mode, reason, reset_traffic):
    url = os.environ.get('TEST_REMNAWAVE_URL')
    token_file = os.environ.get('TEST_REMNAWAVE_TOKEN_FILE')
    if not url or not token_file:
        pytest.skip('Explicit disposable loopback Remnawave URL/token required')
    assert urlparse(url).hostname in {'localhost', '127.0.0.1', '::1'}
    token = (await asyncio.to_thread(Path(token_file).read_text)).strip()
    monkeypatch.setattr(settings, 'REMNAWAVE_API_URL', url)
    monkeypatch.setattr(settings, 'REMNAWAVE_API_KEY', token)
    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_API_FALLBACK_ENABLED', False)
    squads = []
    external = []
    account_id = None
    async with RemnaWaveAPI(url, token) as api:
        try:
            for _ in range(2):
                response = await api._make_request(
                    'POST', '/api/internal-squads', {'name': 'test-' + secrets.token_hex(5), 'inbounds': []}
                )
                squads.append(response['response']['uuid'])
                external.append((await api.create_external_squad('test-' + secrets.token_hex(5))).uuid)
            response = await api._make_request(
                'POST', '/api/internal-squads', {'name': 'test-' + secrets.token_hex(5), 'inbounds': []}
            )
            squads.append(response['response']['uuid'])
            now = datetime.now(UTC)
            response = await api._make_request(
                'POST',
                '/api/users',
                {
                    'username': 'test-' + secrets.token_hex(6),
                    'expireAt': (now + timedelta(days=30)).isoformat(),
                    'activeInternalSquads': [squads[0]],
                    'externalSquadUuid': external[0],
                    'trafficLimitBytes': 10 * 1024**3,
                    'hwidDeviceLimit': 5,
                },
            )
            account_id = response['response']['id']
            gateway = RemnawaveGracePanelGateway()
            configured = {'detach': None, 'keep': 'keep', 'replace': external[1]}[external_mode]
            expected_external = {'detach': None, 'keep': external[0], 'replace': external[1]}[external_mode]
            policy = GraceAccessPolicy(
                duration=timedelta(hours=12),
                expired_squad_uuid=squads[1],
                limited_squad_uuid=squads[2],
                external_squad_uuid=configured,
            )
            for cycle in range(2):
                snapshot = await gateway.read_snapshot(account_id)
                assert snapshot.squad_uuids == (squads[0],)
                assert snapshot.external_squad_uuid == external[0]
                overlay = build_panel_overlay(
                    snapshot, reason, policy, now=datetime.now(UTC), reset_used_traffic=reset_traffic
                )
                await gateway.apply_overlay(account_id, overlay)
                active = await gateway.read_snapshot(account_id)
                assert active.squad_uuids == (squads[1 if reason is GraceReason.EXPIRED else 2],)
                assert active.external_squad_uuid == expected_external
                assert active.status == 'active'
                billing = GraceBillingState(
                    subscription_id=1,
                    remnawave_id=account_id,
                    status='active',
                    end_at=now + timedelta(days=30 * (cycle + 1)),
                    traffic_limit_bytes=10 * 1024**3,
                    used_traffic_bytes=0,
                    device_limit=5,
                    squad_uuids=(squads[0],),
                    external_squad_uuid=external[0],
                )
                await gateway.apply_billing_state(billing, expected_overlay=overlay)
                paid = await gateway.read_snapshot(account_id)
                assert paid.squad_uuids == (squads[0],)
                assert paid.external_squad_uuid == external[0]
                assert paid.traffic_limit_bytes == billing.traffic_limit_bytes
                assert paid.status == 'active'
        finally:
            if account_id is not None:
                await api.delete_user(account_id)
            for uuid in squads:
                await api._make_request('DELETE', '/api/internal-squads/' + uuid)
            for uuid in external:
                await api.delete_external_squad(uuid)
