"""Deleted grace squads must be replaced safely on a disposable real panel."""

import asyncio
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import pytest

from app.config import settings
from app.database.models import Base, Subscription, User
from app.external.remnawave_api import RemnaWaveAPI
from app.services import grace_access_runtime as rt
from app.services.grace_access_service import (
    GraceAccessMode,
    GraceAccessSession,
    GraceBillingState,
    GracePanelOverlay,
    GraceReason,
    GraceSessionState,
)
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('active', [False, True])
@pytest.mark.parametrize(
    'failure',
    [
        'none',
        'lost_reply',
        'lost_commit',
        'lost_commit_drain',
        'lost_commit_timeout',
        'drain',
        'paid',
        'no_replacement',
    ],
)
@pytest.mark.parametrize('reset_traffic', [False, True])
@pytest.mark.parametrize('reason', [GraceReason.EXPIRED, GraceReason.LIMITED])
async def test_replaced_grace_squad_recovers_without_new_entitlement(
    postgres_database, monkeypatch, active, failure, reset_traffic, reason
):
    url = os.environ.get('TEST_REMNAWAVE_URL')
    token_file = os.environ.get('TEST_REMNAWAVE_TOKEN_FILE')
    if not url or not token_file:
        pytest.skip('Disposable loopback panel required')
    assert urlparse(url).hostname in {'localhost', '127.0.0.1', '::1'}
    token = (await asyncio.to_thread(Path(token_file).read_text)).strip()
    monkeypatch.setattr(settings, 'REMNAWAVE_API_URL', url)
    monkeypatch.setattr(settings, 'REMNAWAVE_API_KEY', token)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(rt.grace_access_runtime, '_mode', GraceAccessMode.ACTIVE)
    squads = []
    panel_id = None
    async with RemnaWaveAPI(url, token) as api:
        try:
            for _ in range(3):
                response = await api._make_request(
                    'POST', '/api/internal-squads', {'name': 'test-' + secrets.token_hex(5), 'inbounds': []}
                )
                squads.append(response['response']['uuid'])
            regular, old, replacement = squads
            now = datetime.now(UTC)
            paid_end = now - timedelta(days=1) if reason is GraceReason.EXPIRED else now + timedelta(days=10)
            deadline = now + (timedelta(seconds=8) if failure == 'real_timeout' else timedelta(hours=3))
            response = await api._make_request(
                'POST',
                '/api/users',
                {
                    'username': 'test-' + secrets.token_hex(5),
                    'expireAt': (paid_end if failure == 'real_timeout' else deadline).isoformat(),
                    'status': 'EXPIRED' if failure == 'real_timeout' else 'DISABLED',
                    'activeInternalSquads': [regular],
                    'trafficLimitBytes': 10 * 1024**3,
                },
            )
            panel_id = response['response']['id']
            gateway = rt.RemnawaveGracePanelGateway()
            snapshot = await gateway.read_snapshot(panel_id)
            overlay = GracePanelOverlay(
                status='ACTIVE',
                expire_at=deadline,
                traffic_limit_bytes=1024**3,
                squad_uuids=(old,),
                reset_used_traffic=reset_traffic,
            )
            async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, observer):
                db.add(User(id=1, telegram_id=1, status='active'))
                db.add(
                    Subscription(
                        id=1,
                        user_id=1,
                        remnawave_id=panel_id,
                        status=reason.value,
                        is_trial=False,
                        start_date=now - timedelta(days=31),
                        end_date=paid_end,
                        traffic_limit_gb=10,
                        traffic_used_gb=10 if reason is GraceReason.LIMITED else 0,
                        connected_squads=[regular],
                    )
                )
                await db.commit()
                store = rt.SQLAlchemyGraceSessionStore(db, subscription_id=1)
                session = await store.create(
                    GraceAccessSession(
                        id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
                        subscription_id=1,
                        remnawave_id=panel_id,
                        reason=reason,
                        incident_key='expired:test',
                        state=GraceSessionState.ACTIVE if active else GraceSessionState.PENDING,
                        billing_before=GraceBillingState(
                            subscription_id=1,
                            remnawave_id=panel_id,
                            status=reason.value,
                            end_at=paid_end,
                            traffic_limit_bytes=10 * 1024**3,
                            used_traffic_bytes=10 * 1024**3 if reason is GraceReason.LIMITED else 0,
                            device_limit=1,
                            squad_uuids=(regular,),
                        ),
                        panel_before=snapshot,
                        overlay=overlay,
                        started_at=now,
                        grace_until=deadline,
                        updated_at=now,
                    )
                )
                await db.commit()
                if active:
                    await gateway.apply_overlay(panel_id, overlay)
                await api._make_request('DELETE', '/api/internal-squads/' + old)
                squads.remove(old)
                monkeypatch.setattr(settings, 'GRACE_ACCESS_EXPIRED_SQUAD_UUID', replacement)
                monkeypatch.setattr(settings, 'GRACE_ACCESS_LIMITED_SQUAD_UUID', replacement)
                if failure in {'drain', 'paid', 'no_replacement'}:
                    # Recovery/teardown must not depend on a usable replacement.
                    monkeypatch.setattr(settings, 'GRACE_ACCESS_EXPIRED_SQUAD_UUID', old)
                    monkeypatch.setattr(settings, 'GRACE_ACCESS_LIMITED_SQUAD_UUID', old)
                    if failure == 'paid':
                        sub = await db.get(Subscription, 1)
                        sub.status = 'active'
                        sub.end_date = now + timedelta(days=30)
                        await db.commit()
                    core = rt._build_core(db, subscription_id=1)
                    result = await core.drain(force_restore=True) if failure == 'drain' else await core.reconcile()
                    await db.commit()
                    if failure == 'no_replacement':
                        result = await rt._build_core(observer, subscription_id=1).reconcile()
                        await observer.commit()
                        assert result.errors == 1
                        saved = await store.get_open(1)
                        assert saved is not None
                        assert 'GraceSquadReplacementRequired' in saved.last_error
                        assert saved.grace_until == deadline
                        assert (await gateway.read_snapshot(panel_id)).squad_uuids != (replacement,)
                    else:
                        assert result.errors == 0
                        assert await store.get_open(1) is None
                        actual = await gateway.read_snapshot(panel_id)
                        assert actual.squad_uuids == (regular,)
                        assert actual.status == ('disabled' if failure == 'drain' else 'active')
                        assert actual.traffic_limit_bytes == 10 * 1024**3
                    return
                original_request = RemnaWaveAPI._make_request
                reset_calls = []
                injected = False

                async def request(client, method, path, data=None, **kwargs):
                    nonlocal injected
                    result = await original_request(client, method, path, data, **kwargs)
                    if 'reset-traffic' in path:
                        reset_calls.append(path)
                    if (
                        failure == 'lost_reply'
                        and not injected
                        and method == 'PATCH'
                        and path == '/api/users'
                        and data
                        and data.get('activeInternalSquads') == [replacement]
                    ):
                        injected = True
                        raise TimeoutError('Reply lost after real panel committed replacement')
                    return result

                with monkeypatch.context() as patch:
                    patch.setattr(RemnaWaveAPI, '_make_request', request)
                    result = await rt._build_core(db, subscription_id=1).reconcile()
                    if failure in {'lost_commit', 'lost_commit_drain', 'lost_commit_timeout', 'real_timeout'}:
                        assert result.errors == 0
                        await db.rollback()
                    else:
                        await db.commit()
                    # A new core and independent DB session model a worker restart.
                    restarted = rt._build_core(observer, subscription_id=1)
                    if failure == 'real_timeout':
                        # Wait for the real panel's scheduler, not a patched clock.
                        async with asyncio.timeout(90):
                            while True:
                                actual = await gateway.read_snapshot(panel_id)
                                if actual.status == 'expired' and datetime.now(UTC) >= deadline:
                                    break
                                await asyncio.sleep(2)
                        result = await restarted.reconcile()
                    elif failure == 'lost_commit_drain':
                        result = await restarted.drain(force_restore=True)
                    elif failure == 'lost_commit_timeout':
                        restarted._clock = lambda: deadline + timedelta(seconds=1)
                        result = await restarted.reconcile()
                    else:
                        result = await restarted.reconcile()
                    await observer.commit()
                assert result.errors == 0
                if active:
                    assert reset_calls == []
                if failure in {'lost_commit_drain', 'lost_commit_timeout', 'real_timeout'}:
                    assert await store.get_open(1) is None
                    actual = await gateway.read_snapshot(panel_id)
                    assert actual.squad_uuids == (regular,)
                    assert actual.status == ('expired' if failure == 'real_timeout' else 'disabled')
                    assert actual.traffic_limit_bytes == 10 * 1024**3
                    return
                saved = await store.get_open(1)
                assert saved is not None and saved.state is GraceSessionState.ACTIVE
                assert saved.overlay.squad_uuids == (replacement,)
                assert saved.grace_until == session.grace_until
                assert saved.panel_before == snapshot
                assert (await observer.get(Subscription, 1)).end_date == paid_end
                actual = await gateway.read_snapshot(panel_id)
                assert actual.squad_uuids == (replacement,)
                assert actual.traffic_limit_bytes == overlay.traffic_limit_bytes
                assert abs((actual.expire_at - deadline).total_seconds()) < 1
        finally:
            if panel_id is not None:
                await api.delete_user(panel_id)
            for uuid in squads:
                await api._make_request('DELETE', '/api/internal-squads/' + uuid)


async def test_natural_panel_expiry_after_lost_commit(postgres_database, monkeypatch):
    await test_replaced_grace_squad_recovers_without_new_entitlement(
        postgres_database,
        monkeypatch,
        active=True,
        failure='real_timeout',
        reset_traffic=False,
        reason=GraceReason.EXPIRED,
    )
