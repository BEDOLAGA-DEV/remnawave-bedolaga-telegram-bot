"""Anore wire contract: real local HTTP, no external payment calls."""

import asyncio
import hashlib
import hmac
import json
from dataclasses import replace

import pytest
import pytest_asyncio
from aiohttp import web

from app.services.anore_service import AnoreAPIError, AnoreConfig, AnoreService, parse_rubles_to_kopeks


@pytest_asyncio.fixture
async def anore_http(unused_tcp_port):
    calls = []
    reply = {'status': 200, 'body': {'success': True, 'id': 'invoice-1'}, 'headers': {}}

    async def handler(request):
        calls.append((request.method, request.path, dict(request.headers), await request.read()))
        if reply.get('chunks'):
            response = web.StreamResponse()
            await response.prepare(request)
            for chunk in reply['chunks']:
                await response.write(chunk)
                await asyncio.sleep(0.01)
            return response
        if reply.get('delay'):
            await asyncio.sleep(reply['delay'])
        if isinstance(reply['body'], str):
            return web.Response(text=reply['body'], status=reply['status'], headers=reply['headers'])
        return web.json_response(reply['body'], status=reply['status'], headers=reply['headers'])

    app = web.Application()
    app.router.add_route('*', '/v1/{path:.*}', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', unused_tcp_port).start()
    config = AnoreConfig(
        api_url=f'http://127.0.0.1:{unused_tcp_port}/v1',
        api_key='an_live_synthetic',
        secret_key='synthetic-secret',
        shop_id=7,
    )
    try:
        yield AnoreService(), config, calls, reply
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_create_signs_exact_wire_bytes_and_uses_ruble_units(anore_http):
    service, config, calls, _ = anore_http
    result = await service.create_payment(
        amount_kopeks=101,
        description='Тест',
        order_id='order-1',
        methods=['sbp', 'card'],
        email='',
        success_url='https://cabinet.example/success',
        fail_url='https://cabinet.example/fail',
        callback_url='https://bot.example/anore-webhook',
        config=config,
    )
    assert result['id'] == 'invoice-1'
    method, path, headers, raw = calls[0]
    assert (method, path) == ('POST', '/v1/payments')
    assert headers['Authorization'] == 'Bearer an_live_synthetic'
    assert headers['Anore-Signature'] == hmac.new(b'synthetic-secret', raw, hashlib.sha256).hexdigest()
    assert json.loads(raw) == {
        'amount': 1.01,
        'currency': 'RUB',
        'description': 'Тест',
        'orderId': 'order-1',
        'shopId': 7,
        'methods': ['sbp', 'card'],
        'successurl': 'https://cabinet.example/success',
        'failurl': 'https://cabinet.example/fail',
        'callbackUrl': 'https://bot.example/anore-webhook',
    }


@pytest.mark.asyncio
async def test_get_uses_saved_id_and_snapshot_credentials(anore_http):
    service, config, calls, _ = anore_http
    await service.get_payment('invoice-1', config=config)
    assert calls[0][:2] == ('GET', '/v1/payments/invoice-1')
    assert calls[0][2]['Authorization'] == 'Bearer an_live_synthetic'


@pytest.mark.parametrize('code', [400, 401, 403, 404, 429, 503])
@pytest.mark.asyncio
async def test_errors_are_safe_and_creation_is_never_retried(anore_http, code):
    service, config, calls, reply = anore_http
    reply.update(status=code, body={'error': 'an_live_synthetic synthetic-secret'}, headers={'Retry-After': '12'})
    with pytest.raises(AnoreAPIError) as err:
        await service.create_payment(amount_kopeks=100, description='Test', order_id='order-1', config=config)
    assert err.value.status_code == code
    assert err.value.retry_after == 12
    assert 'synthetic' not in str(err.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    'body', ['not json', [], {'success': False}, {'success': 'true'}, {'success': True, 'x': float('nan')}]
)
@pytest.mark.asyncio
async def test_invalid_responses_are_rejected(anore_http, body):
    service, config, calls, reply = anore_http
    reply['body'] = body
    with pytest.raises(AnoreAPIError):
        await service.get_payment('invoice-1', config=config)
    assert len(calls) == 1


@pytest.mark.parametrize('value,expected', [(1, 100), ('1.01', 101), (0.29, 29), ('12345.67', 1234567)])
def test_exact_ruble_conversion(value, expected):
    assert parse_rubles_to_kopeks(value) == expected


@pytest.mark.parametrize('value', [True, False, None, '', 'nan', 'Infinity', '-1', '1.001', [], {}, '1e9999'])
def test_invalid_money_rejected(value):
    with pytest.raises(ValueError):
        parse_rubles_to_kopeks(value)


@pytest.mark.parametrize('amount', [True, False, 0, -1, 1.01, '100'])
@pytest.mark.asyncio
async def test_invalid_local_amount_does_not_send(anore_http, amount):
    service, config, calls, _ = anore_http
    with pytest.raises(ValueError):
        await service.create_payment(amount_kopeks=amount, description='Test', order_id='order-1', config=config)
    assert not calls


def test_signature_is_over_raw_bytes_and_missing_secret_fails_closed():
    raw = b'{"amount":1, "description":"test"}'
    signature = hmac.new(b'synthetic', raw, hashlib.sha256).hexdigest()
    assert AnoreService.verify_callback_signature(raw, signature, secret='synthetic')
    assert not AnoreService.verify_callback_signature(raw.replace(b' ', b''), signature, secret='synthetic')
    assert not AnoreService.verify_callback_signature(raw, '', secret='synthetic')
    assert not AnoreService.verify_callback_signature(raw, signature, secret='')


@pytest.mark.asyncio
async def test_snapshot_does_not_expose_secrets_and_test_mode_is_explicit(anore_http):
    _, config, _, _ = anore_http
    assert not config.is_test
    assert replace(config, api_key='an_test_synthetic').is_test
    assert 'an_live_synthetic' not in repr(config)
    assert 'synthetic-secret' not in repr(config)


@pytest.mark.asyncio
async def test_chunked_json_is_read_completely(anore_http):
    service, config, _, reply = anore_http
    reply['chunks'] = [b'{"success":', b'true,"id":"invoice-1"}']
    assert (await service.get_payment('invoice-1', config=config))['id'] == 'invoice-1'


@pytest.mark.asyncio
async def test_timeout_does_not_retry_create(anore_http):
    service, config, calls, reply = anore_http
    reply['delay'] = 0.05
    with pytest.raises(AnoreAPIError):
        await service.create_payment(
            amount_kopeks=100, description='Test', order_id='order-1', config=replace(config, timeout=0.01)
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_response_preserves_fractional_kopeks_before_validation(anore_http):
    service, config, _, reply = anore_http
    reply['body'] = '{"success":true,"amount":100.000000000000001}'
    result = await service.get_payment('invoice-1', config=config)
    with pytest.raises(ValueError):
        parse_rubles_to_kopeks(result['amount'])
