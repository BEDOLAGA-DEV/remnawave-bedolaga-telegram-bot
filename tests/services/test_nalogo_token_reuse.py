import json
import os
import stat
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import web

from app.lib.nalogo import Client
from app.services.nalogo_service import NaloGoService


pytestmark = pytest.mark.asyncio


def token(value='local-access', expires=60):
    return {
        'token': value,
        'refreshToken': 'local-refresh',
        'tokenExpireIn': (datetime.now(UTC) + timedelta(minutes=expires)).isoformat(),
        'profile': {'inn': '100000000000'},
    }


@pytest_asyncio.fixture
async def api():
    state = SimpleNamespace(
        login_status=422, refresh_status=200, accepted='local-access', requests=[], income_status=200
    )

    async def respond(request):
        body = await request.json() if request.method == 'POST' else None
        state.requests.append((request.path, request.headers.get('Authorization'), body))
        if request.path.endswith('/auth/lkfl'):
            return web.json_response(
                token('password-access') if state.login_status == 200 else {'code': 'entity.not.found'},
                status=state.login_status,
            )
        if request.path.endswith('/auth/token'):
            state.accepted = 'refreshed-access'
            return web.json_response(
                token('refreshed-access') if state.refresh_status == 200 else {}, status=state.refresh_status
            )
        if request.headers.get('Authorization') not in {f'Bearer {state.accepted}', 'Bearer password-access'}:
            return web.json_response({}, status=401)
        if request.path.endswith('/user'):
            return web.json_response({'inn': '100000000000'})
        return web.json_response(
            {'approvedReceiptUuid': 'local-receipt'}
            if state.income_status == 200
            else {'message': 'upstream unavailable'},
            status=state.income_status,
        )

    app = web.Application()
    app.router.add_route('*', '/api/v1/{path:.*}', respond)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    try:
        await site.start()
        host, port = runner.addresses[0]
        yield f'http://{host}:{port}/api', state
    finally:
        await runner.cleanup()


def service(url, storage):
    result = NaloGoService.__new__(NaloGoService)
    result.client = Client(base_url=url, storage_path=str(storage), timeout=1)
    result.configured = True
    result.inn, result.password = 'local-inn', 'local-password'
    return result


async def test_valid_saved_token_authenticates_without_password_login(api, tmp_path):
    url, state = api
    storage = tmp_path / 'tokens.json'
    storage.write_text(json.dumps(token()))
    assert await service(url, storage).authenticate() is True
    assert all(not path.endswith('/auth/lkfl') for path, _, _ in state.requests)


async def test_receipts_use_live_in_memory_token_instead_of_password(api, tmp_path):
    url, state = api
    storage = tmp_path / 'tokens.json'
    storage.write_text(json.dumps(token()))
    instance = service(url, storage)
    assert await instance.create_receipt('Local service', 1, queue_on_failure=False) == 'local-receipt'
    assert [path for path, _, _ in state.requests] == ['/api/v1/income']


async def test_expired_saved_token_refreshes_and_persists_new_token(api, tmp_path):
    url, state = api
    storage = tmp_path / 'tokens.json'
    storage.write_text(json.dumps(token(expires=-1)))
    assert await service(url, storage).authenticate() is True
    assert [path for path, _, _ in state.requests] == ['/api/v1/auth/token', '/api/v1/user']
    assert json.loads(storage.read_text())['token'] == 'refreshed-access'


async def test_revoked_token_and_dead_refresh_fall_back_to_password(api, tmp_path):
    url, state = api
    state.accepted, state.refresh_status, state.login_status = 'other-access', 401, 200
    storage = tmp_path / 'tokens.json'
    storage.write_text(json.dumps(token()))
    assert await service(url, storage).authenticate() is True
    assert any(path.endswith('/auth/token') for path, _, _ in state.requests)
    assert any(path.endswith('/auth/lkfl') for path, _, _ in state.requests)


@pytest.mark.parametrize('contents', [None, 'broken-json'])
async def test_no_usable_storage_retains_password_login(api, tmp_path, contents):
    url, state = api
    state.login_status = 200
    storage = tmp_path / 'tokens.json'
    if contents is not None:
        storage.write_text(contents)
    assert await service(url, storage).authenticate() is True
    assert [path for path, _, _ in state.requests] == ['/api/v1/auth/lkfl']


async def test_explicit_authentication_rereads_new_storage(api, tmp_path):
    url, state = api
    storage = tmp_path / 'tokens.json'
    instance = service(url, storage)
    storage.write_text(json.dumps(token()))
    assert await instance.authenticate() is True
    assert all(not path.endswith('/auth/lkfl') for path, _, _ in state.requests)


async def test_token_file_is_private_after_atomic_save(tmp_path):
    storage = tmp_path / 'tokens.json'
    client = Client(storage_path=str(storage))
    payload = token()
    old_umask = os.umask(0o022)
    try:
        await client.authenticate(json.dumps(payload))
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(storage.stat().st_mode) == 0o600
    assert json.loads(storage.read_text()) == payload


async def test_revoked_in_memory_access_recovers_before_giving_up_receipt(api, tmp_path):
    url, state = api
    state.accepted, state.refresh_status, state.login_status = 'other-access', 401, 200
    storage = tmp_path / 'tokens.json'
    storage.write_text(json.dumps(token()))
    assert await service(url, storage).create_receipt('Local service', 1, queue_on_failure=False) == 'local-receipt'
    paths = [path for path, _, _ in state.requests]
    assert paths.count('/api/v1/income') == 2
    assert paths.count('/api/v1/auth/lkfl') == 1


@pytest_asyncio.fixture
async def receipt_cache(monkeypatch):
    from uuid import uuid4

    from app.services import nalogo_queue_service, nalogo_service
    from app.utils.cache import CacheService
    from tests.fixtures.real_redis import ensure_real_redis

    url = os.environ.get('TEST_CART_REDIS_URL')
    if not url:
        if os.environ.get('REQUIRE_REDIS_TESTS') == '1':
            pytest.fail('TEST_CART_REDIS_URL required')
        pytest.skip('isolated Redis not configured')
    client = ensure_real_redis(monkeypatch).create_redis(url)
    await client.ping()
    cache = CacheService()
    cache.redis_client, cache._connected = client, True
    payment_id = f'local-{uuid4().hex}'
    queue_key, pending_key = f'{payment_id}:queue', f'{payment_id}:pending'
    monkeypatch.setattr(nalogo_service, 'cache', cache)
    monkeypatch.setattr(nalogo_queue_service, 'cache', cache)
    monkeypatch.setattr(nalogo_service, 'NALOGO_QUEUE_KEY', queue_key)
    monkeypatch.setattr(nalogo_service, 'NALOGO_PENDING_VERIFICATION_KEY', pending_key)
    monkeypatch.setattr(nalogo_queue_service.NalogoQueueService, '_receipt_delay', property(lambda _: 0))
    try:
        yield cache, payment_id
    finally:
        await client.delete(queue_key, pending_key, f'nalogo:created:{payment_id}', f'nalogo:queued:{payment_id}')
        await client.aclose()


async def test_ambiguous_queued_receipt_stays_manual_without_repeated_post(api, tmp_path, receipt_cache):
    from app.services.nalogo_queue_service import NalogoQueueService

    url, state = api
    _, payment_id = receipt_cache
    state.income_status = 503
    storage = tmp_path / 'tokens.json'
    storage.write_text(json.dumps(token()))
    instance = service(url, storage)
    await instance._queue_receipt('Local service', 1, 1, None, payment_id, None, 100)
    worker = NalogoQueueService(instance)
    await worker._process_pending_receipts()
    assert await instance.get_pending_verification_count() == 1
    assert await instance.get_queue_length() == 0
    # A repeated payment callback must also leave the ambiguous request alone.
    assert await instance.create_receipt('Local service', 1, payment_id=payment_id) is None
    await worker._process_pending_receipts()
    assert sum(path.endswith('/income') for path, _, _ in state.requests) == 1
    # Only the explicit admin action after checking the external service may retry.
    state.income_status = 200
    assert await instance.retry_pending_receipt(payment_id) == 'local-receipt'
    assert await instance.get_pending_verification_count() == 0


async def test_manual_confirmation_without_uuid_blocks_receipt_and_stale_queue(api, tmp_path, receipt_cache):
    from app.services.nalogo_queue_service import NalogoQueueService

    url, state = api
    cache, payment_id = receipt_cache
    instance = service(url, tmp_path / 'tokens.json')
    await instance._queue_receipt('Local service', 1, 1, None, payment_id, None, 100)
    await instance._save_pending_verification('Local service', 1, 1, None, payment_id, None, 100, 'timeout')
    assert await instance.mark_pending_as_verified(payment_id, was_created=True)
    assert await cache.get(f'nalogo:created:{payment_id}')
    assert await instance.create_receipt('Local service', 1, payment_id=payment_id) is None
    await NalogoQueueService(instance)._process_pending_receipts()
    assert await instance.get_queue_length() == 0
    assert state.requests == []


@pytest.mark.parametrize('queued', [False, 'legacy', 'current'])
async def test_manual_retry_preserves_original_operation_time(api, tmp_path, receipt_cache, queued):
    from app.services.nalogo_queue_service import NalogoQueueService

    url, state = api
    cache, payment_id = receipt_cache
    storage = tmp_path / 'tokens.json'
    storage.write_text(json.dumps(token()))
    instance = service(url, storage)
    state.income_status = 503
    operation_time = datetime(2026, 9, 10, 12, 30, tzinfo=UTC)
    if queued:
        # Legacy queued records already have their original creation time.
        await instance._queue_receipt('Local service', 1, 1, None, payment_id, None, 100, operation_time=operation_time)
        from app.services.nalogo_service import NALOGO_QUEUE_KEY

        entries = await cache.lrange(NALOGO_QUEUE_KEY)
        entries[0]['created_at'] = operation_time.isoformat()
        if queued == 'legacy':
            entries[0].pop('operation_time', None)
        await cache.delete(NALOGO_QUEUE_KEY)
        await cache.lpush(NALOGO_QUEUE_KEY, entries[0])
        await NalogoQueueService(instance)._process_pending_receipts()
    else:
        await instance.create_receipt('Local service', 1, payment_id=payment_id, operation_time=operation_time)
    state.income_status = 200
    assert await instance.retry_pending_receipt(payment_id) == 'local-receipt'
    bodies = [body for path, _, body in state.requests if path.endswith('/income')]
    assert len(bodies) == 2
    assert datetime.fromisoformat(bodies[0]['operationTime']) == operation_time
    assert datetime.fromisoformat(bodies[1]['operationTime']) == operation_time


async def test_failed_atomic_replace_preserves_previous_file_and_cleans_temp(tmp_path, monkeypatch, caplog):
    from app.lib.nalogo import auth

    storage = tmp_path / 'tokens.json'
    previous = token('previous-local-access')
    storage.write_text(json.dumps(previous))
    provider = service('http://127.0.0.1/api', storage).client.auth_provider

    def unavailable(*args):
        raise OSError('local replacement denied')

    monkeypatch.setattr(auth.os, 'replace', unavailable)
    await provider.set_token(json.dumps(token('new-local-access')))
    assert json.loads(storage.read_text()) == previous
    assert (await provider.get_token())['token'] == 'new-local-access'
    assert list(tmp_path.iterdir()) == [storage]
    assert 'Failed to persist' in caplog.text
    assert 'new-local-access' not in caplog.text


async def test_concurrent_authentication_refreshes_expired_token_only_once(api, tmp_path):
    import asyncio

    url, state = api
    storage = tmp_path / 'tokens.json'
    storage.write_text(json.dumps(token(expires=-1)))
    instance = service(url, storage)
    assert await asyncio.gather(instance.authenticate(), instance.authenticate()) == [True, True]
    assert sum(path.endswith('/auth/token') for path, _, _ in state.requests) == 1
    assert all(not path.endswith('/auth/lkfl') for path, _, _ in state.requests)


@pytest.mark.parametrize('expiry', [None, 'invalid-date', (datetime.now(UTC) + timedelta(minutes=2)).isoformat()])
async def test_missing_invalid_or_near_expiry_refreshes_instead_of_password(api, tmp_path, expiry):
    url, state = api
    storage = tmp_path / 'tokens.json'
    storage.write_text(json.dumps(token() | {'tokenExpireIn': expiry}))
    assert await service(url, storage).authenticate() is True
    assert [path for path, _, _ in state.requests] == ['/api/v1/auth/token', '/api/v1/user']
