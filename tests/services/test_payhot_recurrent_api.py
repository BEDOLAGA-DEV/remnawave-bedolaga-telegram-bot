"""Production recurring transport via isolated HTTP; no credentials or external calls."""

import json
from dataclasses import replace

import pytest
import pytest_asyncio
from aiohttp import web

from app.services.payhot_recurrent_api import PayHotRecurrentAPI, generate_opaque_reference, validate_opaque_reference
from app.services.payhot_service import PayHotAPIError, PayHotConfig, PayHotService


IDENTIFIER = '11111111-1111-4111-8111-111111111111'
MANDATE = '22222222-2222-4222-8222-222222222222'


def charge():
    return dict(
        id=IDENTIFIER,
        mandate_id=MANDATE,
        merchant_recurring_charge_reference='renew-example',
        amount_minor='10000',
        currency='RUB',
        payment_method='card',
        status='processing',
    )


def mandate():
    return dict(
        id=MANDATE, merchant_mandate_reference='sub-example', currency='RUB', payment_method='card', status='active'
    )


@pytest_asyncio.fixture
async def wire(unused_tcp_port, monkeypatch):
    calls = []
    reply = {'status': 200, 'body': {'mandate': mandate()}, 'headers': {}}

    async def handle(request):
        calls.append((request.method, request.path, dict(request.headers), await request.read()))
        if isinstance(reply['body'], bytes):
            return web.Response(body=reply['body'], status=reply['status'], headers=reply['headers'])
        return web.json_response(reply['body'], status=reply['status'], headers=reply['headers'])

    app = web.Application()
    app.router.add_route('*', '/api/v2/{path:.*}', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', unused_tcp_port).start()
    monkeypatch.setattr(PayHotService, '_base_url', staticmethod(lambda config: config.base_url))
    config = PayHotConfig(api_key='phk_v2_synthetic', base_url=f'http://127.0.0.1:{unused_tcp_port}/api/v2')
    try:
        yield PayHotRecurrentAPI(), config, calls, reply
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_charge_202_and_get_preserve_idempotency_and_principal(wire):
    api, config, calls, reply = wire
    payload = {k: v for k, v in charge().items() if k not in ('id', 'status')}
    reply.update(status=202, body={'recurring_charge': charge()})
    result = await api.create_recurring_charge(payload, 'synthetic-recurring-1', config=config)
    assert result['recurring_charge']['amount_minor'] == '10000'
    assert json.loads(calls[0][3]) == payload
    assert calls[0][2]['Idempotency-Key'] == 'synthetic-recurring-1'
    reply.update(status=200)
    await api.get_recurring_charge(IDENTIFIER, config=config)
    assert calls[1][:2] == ('GET', f'/api/v2/recurring-charges/{IDENTIFIER}')
    assert calls[1][3] == b''


@pytest.mark.asyncio
async def test_get_and_revoke_mandate(wire):
    api, config, calls, reply = wire
    await api.get_mandate(MANDATE, config=config)
    reply['body'] = {'mandate': {**mandate(), 'status': 'revoked'}, 'already_revoked': False}
    await api.revoke_mandate(MANDATE, 'consent_withdrawn', config=config)
    assert calls[1][:2] == ('POST', f'/api/v2/mandates/{MANDATE}/revoke')
    assert json.loads(calls[1][3]) == {'reason': 'consent_withdrawn'}
    assert 'Idempotency-Key' not in calls[1][2]


@pytest.mark.asyncio
async def test_explicit_consent_enrollment_keeps_ordinary_checkout_guard(wire):
    api, config, calls, reply = wire
    payload = dict(
        merchant_order_reference='order-1',
        amount_minor='10000',
        currency='RUB',
        payment_method='sbp',
        customer={'id': 'user-1'},
        recurring_enrollment={},
    )
    reply.update(
        status=201,
        body={
            'payment': {
                **payload,
                'id': IDENTIFIER,
                'status': 'requires_action',
                'recurring_enrollment': {'status': 'pending'},
            },
            'checkout': {'url': 'https://checkout.example/pay#token=synthetic'},
        },
    )
    with pytest.raises(ValueError):
        await PayHotService().create_payment(payload, 'synthetic-recurring-1', config=config)
    result = await api.create_enrollment_payment(payload, 'synthetic-recurring-1', config=config)
    assert result['checkout']['url'].endswith('#token=synthetic')
    assert json.loads(calls[0][3]) == payload


@pytest.mark.parametrize(
    'change',
    [
        dict(currency='EUR'),
        dict(payment_method='crypto'),
        dict(amount_minor='0'),
        dict(amount_minor=10000),
        dict(merchant_recurring_charge_reference=IDENTIFIER),
        dict(mandate_id='../other'),
    ],
)
@pytest.mark.asyncio
async def test_invalid_charge_never_sent(wire, change):
    api, config, calls, _ = wire
    payload = {k: v for k, v in charge().items() if k not in ('id', 'status')}
    with pytest.raises(ValueError):
        await api.create_recurring_charge({**payload, **change}, 'synthetic-recurring-1', config=config)
    assert not calls


@pytest.mark.parametrize(
    'method,customer,consent',
    [
        ('sberpay', {'id': 'user-1'}, {}),
        ('card', {'id': 'a' * 51}, {}),
        ('card', None, {}),
        ('card', {'id': 'user-1'}, True),
    ],
)
@pytest.mark.asyncio
async def test_invalid_enrollment_never_sent(wire, method, customer, consent):
    api, config, calls, _ = wire
    with pytest.raises(ValueError):
        await api.create_enrollment_payment(
            dict(
                merchant_order_reference='order-1',
                amount_minor='10000',
                currency='RUB',
                payment_method=method,
                customer=customer,
                recurring_enrollment=consent,
            ),
            'synthetic-recurring-1',
            config=config,
        )
    assert not calls


@pytest.mark.parametrize(
    'body',
    [
        {'recurring_charge': {**charge(), 'currency': 'EUR'}},
        charge(),
        {'recurring_charge': {**charge(), 'mandate_id': 'invalid'}},
        {'recurring_charge': {**charge(), 'status': 'authorized'}},
    ],
)
@pytest.mark.asyncio
async def test_invalid_envelope_rejected(wire, body):
    api, config, _, reply = wire
    reply['body'] = body
    with pytest.raises(PayHotAPIError):
        await api.get_recurring_charge(IDENTIFIER, config=config)


@pytest.mark.asyncio
async def test_test_key_never_sent(wire):
    api, config, calls, _ = wire
    with pytest.raises(ValueError):
        await api.get_mandate(MANDATE, config=replace(config, api_key='phk_test_v2_synthetic'))
    assert not calls


def test_generated_references_follow_actual_provider_regex():
    values = {generate_opaque_reference('renew') for _ in range(1000)}
    assert len(values) == 1000
    assert all(validate_opaque_reference(value) == value for value in values)
    with pytest.raises(ValueError):
        validate_opaque_reference('renew-' + '1' * 12)


@pytest.mark.parametrize('operation', ['get_charge', 'get_mandate', 'create_charge'])
@pytest.mark.asyncio
async def test_response_identity_must_match_requested_operation(wire, operation):
    api, config, _, reply = wire
    if operation == 'get_mandate':
        reply['body'] = {'mandate': {**mandate(), 'id': IDENTIFIER}}
        call = api.get_mandate(MANDATE, config=config)
    else:
        reply.update(
            status=202 if operation == 'create_charge' else 200,
            body={'recurring_charge': {**charge(), 'id': MANDATE, 'amount_minor': '20000'}},
        )
        if operation == 'get_charge':
            call = api.get_recurring_charge(IDENTIFIER, config=config)
        else:
            call = api.create_recurring_charge(
                {k: v for k, v in charge().items() if k not in ('id', 'status')}, 'synthetic-recurring-1', config=config
            )
    with pytest.raises(PayHotAPIError):
        await call


@pytest.mark.parametrize('status', [302, 429, 503])
@pytest.mark.asyncio
async def test_errors_do_not_leak_body_or_follow_redirect(wire, status):
    api, config, calls, reply = wire
    reply.update(status=status, body={'secret': 'must-not-leak'}, headers={'Location': 'https://invalid.example/'})
    with pytest.raises(PayHotAPIError) as exc:
        await api.get_mandate(MANDATE, config=config)
    assert 'must-not-leak' not in str(exc.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    'change',
    [
        dict(merchant_order_reference='another-order'),
        dict(amount_minor='20000'),
        dict(payment_method='card'),
        dict(currency='EUR'),
    ],
)
@pytest.mark.asyncio
async def test_enrollment_response_must_match_saved_intent(wire, change):
    api, config, _, reply = wire
    payload = dict(
        merchant_order_reference='order-1',
        amount_minor='10000',
        currency='RUB',
        payment_method='sbp',
        customer={'id': 'user-1'},
        recurring_enrollment={},
    )
    reply.update(status=201, body={'payment': {**payload, 'id': IDENTIFIER, 'status': 'requires_action', **change}})
    with pytest.raises(PayHotAPIError):
        await api.create_enrollment_payment(payload, 'synthetic-recurring-1', config=config)


@pytest.mark.parametrize('method', [[], {}])
@pytest.mark.asyncio
async def test_invalid_method_type_is_validation_error(wire, method):
    api, config, calls, _ = wire
    payload = {k: v for k, v in charge().items() if k not in ('id', 'status')}
    payload['payment_method'] = method
    with pytest.raises(ValueError):
        await api.create_recurring_charge(payload, 'synthetic-recurring-1', config=config)
    assert not calls


@pytest.mark.parametrize('status', ['manual_reconciliation', 'future_unknown_status'])
@pytest.mark.asyncio
async def test_unknown_charge_status_cannot_be_reported_as_failed_or_paid(wire, status):
    api, config, _, reply = wire
    reply['body'] = {'recurring_charge': {**charge(), 'status': status}}
    with pytest.raises(PayHotAPIError) as exc:
        await api.get_recurring_charge(IDENTIFIER, config=config)
    assert exc.value.status_code == 200
