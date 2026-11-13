"""PayHot RUB contract through a loopback HTTP server, synthetic credentials only."""

import hashlib
import hmac
import json
from dataclasses import replace

import pytest
import pytest_asyncio
from aiohttp import web

from app.services.payhot_service import PayHotAPIError, PayHotConfig, PayHotService, verify_signature


PAYMENT_ID = '11111111-1111-4111-8111-111111111111'


def response():
    return {
        'payment': {
            'id': PAYMENT_ID,
            'merchant_order_reference': 'order-1',
            'amount_minor': '10000',
            'currency': 'RUB',
            'status': 'requires_action',
        },
        'checkout': {'url': 'https://checkout.example/pay/id#token=synthetic'},
    }


@pytest_asyncio.fixture
async def wire(unused_tcp_port, monkeypatch):
    calls = []
    reply = {'status': 201, 'body': response(), 'headers': {}}

    async def handler(request):
        calls.append((request.method, request.path, dict(request.headers), await request.read()))
        if isinstance(reply['body'], bytes):
            return web.Response(body=reply['body'], status=reply['status'], headers=reply['headers'])
        return web.json_response(reply['body'], status=reply['status'], headers=reply['headers'])

    app = web.Application()
    app.router.add_route('*', '/api/v2/{path:.*}', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', unused_tcp_port).start()
    # Only the isolated HTTP test bypasses the production origin allowlist.
    monkeypatch.setattr(PayHotService, '_base_url', staticmethod(lambda config: config.base_url))
    config = PayHotConfig(api_key='phk_v2_synthetic', base_url=f'http://127.0.0.1:{unused_tcp_port}/api/v2')
    try:
        yield PayHotService(), config, calls, reply
    finally:
        await runner.cleanup()


@pytest.mark.parametrize('method', ['card', 'sbp', 'sberpay', 'crypto', 'applepay', 'googlepay'])
@pytest.mark.asyncio
async def test_create_all_six_rub_methods_and_preserves_fragment(wire, method):
    service, config, calls, _ = wire
    payload = {
        'merchant_order_reference': 'order-1',
        'amount_minor': '10000',
        'currency': 'RUB',
        'payment_method': method,
    }
    if method == 'sberpay':
        payload['customer'] = {'id': 'user-1', 'phone': '+79990000000', 'sberpay': {'front_type': 'back2app'}}
    result = await service.create_payment(payload, 'synthetic-idempotency-1', config=config)
    assert result['checkout']['url'].endswith('#token=synthetic')
    assert calls[0][:2] == ('POST', '/api/v2/payments')
    assert calls[0][2]['Idempotency-Key'] == 'synthetic-idempotency-1'
    assert json.loads(calls[0][3]) == payload


@pytest.mark.asyncio
async def test_get_reissue_production_and_body(wire):
    service, config, calls, reply = wire
    await service.get_payment(PAYMENT_ID, config=config)
    await service.reissue_checkout(PAYMENT_ID, idempotency_key='synthetic-reissue-1', config=config)
    prefix = '/api/v2/payments'
    assert calls[0][:2] == ('GET', f'{prefix}/{PAYMENT_ID}')
    assert calls[1][:2] == ('POST', f'{prefix}/{PAYMENT_ID}/checkout-sessions')
    assert calls[1][3] == b''
    assert 'Idempotency-Key' not in calls[1][2]
    reply['status'] = 201
    reply['body']['checkout'] = {'access_token_available': False, 'reissue_required': True}
    payload = {
        'merchant_order_reference': 'order-1',
        'amount_minor': '10000',
        'currency': 'RUB',
        'payment_method': 'sbp',
    }
    assert (await service.create_payment(payload, 'synthetic-idempotency-1', config=config))['checkout'][
        'reissue_required'
    ]


@pytest.mark.parametrize(
    'change',
    [
        {'currency': 'EUR'},
        {'amount_minor': 100},
        {'amount_minor': '1.00'},
        {'amount_minor': '0'},
        {'amount_minor': '9223372036854775808'},
        {'payment_method': 'sberpay'},
        {'customer': {'email': 'payer@example.com'}},
    ],
)
@pytest.mark.asyncio
async def test_invalid_payload_never_sends(wire, change):
    service, config, calls, _ = wire
    payload = {
        'merchant_order_reference': 'order-1',
        'amount_minor': '10000',
        'currency': 'RUB',
        'payment_method': 'sbp',
    } | change
    with pytest.raises(ValueError):
        await service.create_payment(payload, 'synthetic-idempotency-1', config=config)
    assert calls == []


@pytest.mark.parametrize('status', [302, 401, 429, 503])
@pytest.mark.asyncio
async def test_errors_never_follow_redirect_or_expose_private_response(wire, status):
    service, config, calls, reply = wire
    reply.update(
        status=status,
        body=b'synthetic-secret #token=synthetic',
        headers={'Location': '/api/v2/leak', 'Retry-After': '12'},
    )
    with pytest.raises(PayHotAPIError) as error:
        await service.get_payment(PAYMENT_ID, config=config)
    assert error.value.status_code == status
    assert error.value.retry_after == 12
    assert 'synthetic' not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize('body', [b'x' * 65537, b'{', b'[]', b'{"payment":null}', b'{"payment":NaN}'])
@pytest.mark.asyncio
async def test_invalid_response_is_bounded_and_sanitized(wire, body):
    service, config, _, reply = wire
    reply.update(status=200, body=body)
    with pytest.raises(PayHotAPIError):
        await service.get_payment(PAYMENT_ID, config=config)


def test_config_repr_never_contains_api_key():
    assert 'phk_v2_synthetic' not in repr(PayHotConfig(api_key='phk_v2_synthetic'))


@pytest.mark.parametrize('offset,valid', [(-301, False), (-300, True), (0, True), (300, True), (301, False)])
def test_signature_uses_full_secret_raw_body_and_timestamp(offset, valid):
    raw, secret, now = b'{"example": 1}\n', 'whsec_v2_synthetic', 1800000000
    stamp = str(now + offset)
    digest = hmac.new(secret.encode(), stamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()
    signature = f't={stamp},v2={digest}'
    assert verify_signature(raw, signature, [secret], now=now) is valid
    assert not verify_signature(raw + b' ', signature, [secret], now=now)
    assert not verify_signature(raw, signature, ['synthetic'], now=now)


@pytest.mark.parametrize(
    'url',
    [
        'http://app.pay.hot/api/v2',
        'https://app.pay.hot.evil.example/api/v2',
        'https://user:secret@app.pay.hot/api/v2',
        'https://app.pay.hot/api/v2?token=x',
    ],
)
def test_untrusted_api_origins_are_rejected(url):
    with pytest.raises(ValueError):
        PayHotService._base_url(PayHotConfig(api_key='phk_v2_synthetic', base_url=url))


@pytest.mark.parametrize('merchant_fee,payer_fee,captured', [(300, 0, None), (0, 300, '10300'), (100, 200, '10200')])
@pytest.mark.asyncio
async def test_fees_do_not_replace_order_principal(wire, merchant_fee, payer_fee, captured):
    service, config, _, reply = wire
    payment = reply['body']['payment']
    payment.update(
        status='succeeded',
        fees={
            'principal_minor': '10000',
            'total_fee_minor': str(merchant_fee + payer_fee),
            'merchant_fee_minor': str(merchant_fee),
            'payer_fee_minor': str(payer_fee),
            'merchant_net_minor': str(10000 - merchant_fee),
        },
    )
    # Synthetic optional fields: the documented GET schema doesn't promise them.
    if captured is not None:
        payment['captured_amount_minor'] = captured
        payment['payer_amount_minor'] = captured
    result = await service.get_payment(PAYMENT_ID, config=config)
    assert result['payment']['amount_minor'] == '10000'
    assert result['payment']['fees'] == payment['fees']
    assert result['payment'].get('captured_amount_minor') == captured


@pytest.mark.parametrize('key', ['phk_test_v2_synthetic', 'invalid'])
@pytest.mark.asyncio
async def test_nonproduction_key_never_sent(wire, key):
    service, config, calls, _ = wire
    with pytest.raises(ValueError):
        await service.get_payment(PAYMENT_ID, config=replace(config, api_key=key))
    assert calls == []


@pytest.mark.asyncio
async def test_get_requires_documented_production_envelope(wire):
    service, config, _, reply = wire
    payment = response()['payment']
    reply.update(status=200, body=payment)
    with pytest.raises(PayHotAPIError):
        await service.get_payment(PAYMENT_ID, config=config)


@pytest.mark.asyncio
async def test_production_preserves_customer_and_durable_intent(wire):
    service, config, calls, _ = wire
    payload = {
        'merchant_order_reference': 'order-1',
        'amount_minor': '10000',
        'currency': 'RUB',
        'payment_method': 'sberpay',
        'customer': {'id': 'user-1', 'phone': '+79990000000', 'sberpay': {'front_type': 'back2app'}},
    }
    await service.create_payment(payload, 'synthetic-idempotency-1', config=config)
    sent = json.loads(calls[0][3])
    assert 'customer' in sent
    assert payload['customer']['phone'] == '+79990000000'
    assert sent['customer'] == payload['customer']
