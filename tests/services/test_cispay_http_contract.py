"""Current CisPay hosted API over a real local HTTP server."""

import asyncio
import hashlib
import hmac
import json
from dataclasses import replace

import pytest
import pytest_asyncio
from aiohttp import web

from app.services.cispay_service import CisPayAPIError, CisPayConfig, CisPayService


@pytest_asyncio.fixture
async def cispay_http(unused_tcp_port):
    calls = []
    reply = {'status': 201, 'body': {'id': 'invoice-1'}, 'headers': {}}

    async def handler(request):
        calls.append((request.method, request.path, dict(request.query), dict(request.headers), await request.read()))
        if reply.get('delay'):
            await asyncio.sleep(reply['delay'])
        if reply.get('chunks'):
            response = web.StreamResponse(status=reply['status'])
            await response.prepare(request)
            for chunk in reply['chunks']:
                await response.write(chunk)
                await asyncio.sleep(0.01)
            return response
        if isinstance(reply['body'], str):
            return web.Response(text=reply['body'], status=reply['status'], headers=reply['headers'])
        return web.json_response(reply['body'], status=reply['status'], headers=reply['headers'])

    app = web.Application()
    app.router.add_route('*', '/{path:.*}', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', unused_tcp_port).start()
    try:
        yield (
            CisPayService(),
            CisPayConfig(
                base_url=f'http://127.0.0.1:{unused_tcp_port}', shop_id='shop-example', api_key='cis_sec_synthetic'
            ),
            calls,
            reply,
        )
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['CARD', 'SBP', 'CRYPTO', None])
async def test_hosted_request_preserves_minor_units_and_metadata(cispay_http, method):
    client, config, calls, _ = cispay_http
    result = await client.create_payment(
        amount_kopeks=10001,
        order_id='order-1',
        payment_method=method,
        customer_id='buyer-1',
        description='Тест',
        payload='opaque-reference',
        config=config,
    )
    assert result['id'] == 'invoice-1'
    assert len(calls) == 1
    verb, path, _, headers, raw = calls[0]
    assert (verb, path) == ('POST', '/payments')
    assert headers['X-Shop-ID'] == config.shop_id and headers['X-Api-Key'] == config.api_key
    data = json.loads(raw)
    assert data['amount'] == 10001 and data['currency'] == 'RUB'
    assert data.get('payment_method') == method
    assert data['payload'] == 'opaque-reference'


@pytest.mark.asyncio
@pytest.mark.parametrize('amount', [True, 0, -1, 10000.9, '10000'])
async def test_invalid_local_money_never_hits_provider(cispay_http, amount):
    client, config, calls, _ = cispay_http
    with pytest.raises(ValueError):
        await client.create_payment(
            amount_kopeks=amount, order_id='order-1', payment_method='SBP', customer_id='buyer-1', config=config
        )
    assert not calls


@pytest.mark.asyncio
async def test_card_minimum_and_customer_required_for_sbp_and_chooser(cispay_http):
    client, config, calls, _ = cispay_http
    for method, amount, customer in [('CARD', 4999, 'buyer'), ('SBP', 10000, ''), (None, 10000, None)]:
        with pytest.raises(ValueError):
            await client.create_payment(
                amount_kopeks=amount, order_id='order-1', payment_method=method, customer_id=customer, config=config
            )
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [301, 400, 401, 403, 429, 503])
async def test_errors_are_safe_and_post_is_never_retried(cispay_http, status):
    client, config, calls, reply = cispay_http
    reply.update(status=status, body={'detail': 'cis_sec_private secret-body'}, headers={'Retry-After': '17'})
    with pytest.raises(CisPayAPIError) as caught:
        await client.create_payment(
            amount_kopeks=10000, order_id='order-1', payment_method='CARD', customer_id='buyer-1', config=config
        )
    assert caught.value.retry_after == 17
    assert 'private' not in str(caught.value) and 'secret-body' not in caught.value.message
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('body', ['[]', '{', '{"amount":NaN}'])
async def test_malformed_response_fails_closed(cispay_http, body):
    client, config, _, reply = cispay_http
    reply.update(status=200, body=body)
    with pytest.raises(CisPayAPIError):
        await client.check_payment(order_id='order-1', config=config)


@pytest.mark.asyncio
async def test_status_preserves_fractional_minor_units_for_strict_validator(cispay_http):
    client, config, calls, reply = cispay_http
    reply.update(status=200, body='{"amount":10000.00000000000001,"status":"PAID"}')
    result = await client.check_payment(order_id='order-1', config=config)
    assert result['amount'] == '10000.00000000000001'
    assert calls[0][2] == {'order_id': 'order-1'}


def test_signature_uses_exact_body_and_missing_key_fails():
    raw = b'{"status":"PAID"}'
    config = CisPayConfig(api_key='secret')
    signature = hmac.new(b'secret', raw, hashlib.sha256).hexdigest()
    client = CisPayService()
    assert client.verify_webhook_signature(raw, signature, config=config)
    assert not client.verify_webhook_signature(raw + b' ', signature, config=config)
    assert not client.verify_webhook_signature(raw, signature, config=replace(config, api_key=''))
    assert 'secret' not in repr(config)


@pytest.mark.asyncio
async def test_timeout_does_not_retry_creating_invoice(cispay_http):
    client, config, calls, reply = cispay_http
    reply['delay'] = 0.05
    with pytest.raises(CisPayAPIError):
        await client.create_payment(
            amount_kopeks=10000, order_id='order-1', payment_method='CARD', config=replace(config, timeout=0.01)
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_chunked_response_is_fully_read(cispay_http):
    client, config, _, reply = cispay_http
    reply.update(status=200, chunks=[b'{"status":', b'"PAID",', b'"amount":10000}'])
    result = await client.check_payment(order_id='order-1', config=config)
    assert result == {'status': 'PAID', 'amount': 10000}


@pytest.mark.asyncio
async def test_oversized_response_is_rejected(cispay_http):
    client, config, _, reply = cispay_http
    reply.update(status=200, body='x' * 1_048_577)
    with pytest.raises(CisPayAPIError):
        await client.check_payment(order_id='order-1', config=config)
