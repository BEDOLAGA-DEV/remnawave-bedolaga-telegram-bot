"""Auth must respect the client timeout, including the service's 30-second budget."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from aiohttp import web

from app.lib.nalogo import Client
from app.services.nalogo_service import NaloGoService


pytestmark = pytest.mark.asyncio

TOKEN = {'token': 'local-access', 'refreshToken': 'local-refresh'}
OPERATIONS = ['password', 'sms_start', 'sms_verify', 'refresh']


@pytest_asyncio.fixture
async def tax_server():
    control = SimpleNamespace(delay=0, requests=[])

    async def respond(request):
        control.requests.append((request.path, await request.json()))
        await asyncio.sleep(control.delay)
        return web.json_response(TOKEN)

    app = web.Application()
    app.router.add_post('/{path:.*}', respond)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    try:
        await site.start()
        host, port = runner.addresses[0]
        yield f'http://{host}:{port}/api', control
    finally:
        await runner.cleanup()


async def authenticate(client, operation):
    if operation == 'password':
        return await client.create_new_access_token('local-inn', 'local-password')
    if operation == 'sms_start':
        return await client.create_phone_challenge('local-phone')
    if operation == 'sms_verify':
        return await client.create_new_access_token_by_phone('local-phone', 'local-challenge', 'local-code')
    return await client.auth_provider.refresh('local-refresh')


@pytest.mark.parametrize('operation', OPERATIONS)
async def test_auth_respects_short_client_timeout(tax_server, operation):
    url, control = tax_server
    control.delay = 0.2
    client = Client(base_url=url, timeout=0.03)
    await client.authenticate(json.dumps({'token': 'previous-access'}))

    if operation == 'refresh':
        assert await authenticate(client, operation) is None
    else:
        with pytest.raises(httpx.ReadTimeout):
            await authenticate(client, operation)

    assert await client.auth_provider.get_token() == {'token': 'previous-access'}
    assert len(control.requests) == 1  # No implicit authentication retries.


@pytest.mark.parametrize('operation', OPERATIONS)
async def test_auth_success_keeps_token_storage_contract(tax_server, tmp_path, operation):
    url, _ = tax_server
    storage = tmp_path / 'tokens.json'
    client = Client(base_url=url, storage_path=str(storage))

    result = await authenticate(client, operation)

    assert (json.loads(result) if isinstance(result, str) else result) == TOKEN
    if operation == 'sms_start':
        assert await client.auth_provider.get_token() is None
        assert not storage.exists()
    else:
        assert await client.auth_provider.get_token() == TOKEN
        assert json.loads(storage.read_text()) == TOKEN


async def test_service_auth_accepts_response_after_ten_seconds(tax_server, tmp_path, monkeypatch):
    url, control = tax_server
    control.delay = 11
    monkeypatch.setattr(
        'app.services.nalogo_service.settings',
        SimpleNamespace(get_nalogo_proxy_url=lambda: None),
    )
    service = NaloGoService(
        inn='local-inn', password='local-password', storage_path=str(tmp_path / 'service-tokens.json')
    )
    assert service.configured
    # Redirect the real service client to a disposable server; no tax API calls.
    service.client.auth_provider.base_url_v1 = f'{url}/v1'

    result = await service.client.create_new_access_token(service.inn, service.password)

    assert json.loads(result) == TOKEN
    assert await service.client.auth_provider.get_token() == TOKEN
    assert len(control.requests) == 1
