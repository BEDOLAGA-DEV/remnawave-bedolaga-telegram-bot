"""Raw PayHot signature and durable callback acknowledgement; synthetic secrets."""

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.webserver import payhot, payments


EVENT_ID = '11111111-1111-4111-8111-111111111111'
SECRET = 'whsec_v2_synthetic'


def headers(raw, secret=SECRET, offset=0):
    stamp = str(int(time.time()) + offset)
    signature = hmac.new(secret.encode(), stamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()
    return {
        'PayHot-Signature': f't={stamp},v2={signature}',
        'PayHot-Event-Id': EVENT_ID,
        'PayHot-Delivery-Id': 'delivery-1',
        'PayHot-Delivery-Attempt': '1',
        'PayHot-Api-Version': 'v2',
    }


@pytest.mark.parametrize(
    'case,status',
    [
        ('ok', 200),
        ('sandbox', 400),
        ('previous', 200),
        ('unconfigured', 500),
        ('missing', 401),
        ('forged', 401),
        ('tampered', 401),
        ('expired', 401),
        ('duplicate_header', 400),
        ('event_id', 400),
        ('version', 400),
        ('environment', 400),
        ('sandbox_missing', 400),
        ('bad_json', 400),
        ('nan', 400),
        ('array', 400),
        ('duplicate_json', 400),
        ('oversize', 413),
        ('failed_save', 500),
        ('validation', 400),
        ('exception', 500),
    ],
)
@pytest.mark.asyncio
async def test_webhook_contract(monkeypatch, case, status):
    monkeypatch.setattr(
        payhot,
        'settings',
        SimpleNamespace(
            PAYHOT_ENABLED=False,
            PAYHOT_WEBHOOK_PATH='/payhot-webhook',
            PAYHOT_WEBHOOK_SECRET='' if case == 'unconfigured' else SECRET,
            PAYHOT_PREVIOUS_WEBHOOK_SECRET='whsec_v2_previous',
        ),
    )
    processor = AsyncMock(return_value=case != 'failed_save')
    if case in ('validation', 'exception'):
        processor.side_effect = (
            ValueError('synthetic-private') if case == 'validation' else RuntimeError('synthetic-private')
        )
    monkeypatch.setattr(payments, '_process_payment_service_callback', processor)
    payload = {
        'api_version': 'v2',
        'event_id': EVENT_ID,
        'event_type': 'payment.succeeded',
        'data': {'id': EVENT_ID, 'object': 'payment', 'status': 'succeeded'},
    }
    if case in ('sandbox', 'sandbox_missing'):
        payload['event_type'] = 'sandbox.payment.succeeded'
        if case == 'sandbox':
            payload['environment'] = 'sandbox'
    if case == 'environment':
        payload['environment'] = 'sandbox'
    raw = json.dumps(payload).encode()
    if case == 'bad_json':
        raw = b'{'
    if case == 'array':
        raw = b'[]'
    if case == 'nan':
        raw = b'{"value":NaN}'
    if case == 'duplicate_json':
        raw = b'{"event_id":"one","event_id":"two"}'
    if case == 'oversize':
        raw = b'x' * 65537
    hdr = headers(raw, 'whsec_v2_previous' if case == 'previous' else SECRET, -301 if case == 'expired' else 0)
    if case == 'missing':
        del hdr['PayHot-Signature']
    if case == 'forged':
        hdr['PayHot-Signature'] = 't=1,v2=' + '0' * 64
    if case == 'tampered':
        raw += b' '
    if case == 'event_id':
        hdr['PayHot-Event-Id'] = 'different'
    if case == 'version':
        hdr['PayHot-Api-Version'] = 'v1'
    request_headers = list(hdr.items())
    if case == 'duplicate_header':
        request_headers.append(('PayHot-Signature', hdr['PayHot-Signature']))
    app = FastAPI()
    payhot.register_payhot_webhook(app, SimpleNamespace())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/payhot-webhook', content=raw, headers=request_headers)
    assert response.status_code == status, response.text
    assert 'synthetic-private' not in response.text
    if case in ('ok', 'previous', 'failed_save', 'validation', 'exception'):
        assert processor.call_args.args[2] == 'process_payhot_callback'
        assert processor.call_args.args[1]['event_id'] == EVENT_ID
    else:
        processor.assert_not_awaited()


def test_disabled_configured_provider_keeps_callback_route(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, 'PAYHOT_API_KEY', 'phk_v2_synthetic')
    monkeypatch.setattr(settings, 'PAYHOT_WEBHOOK_SECRET', SECRET)
    monkeypatch.setattr(settings, 'PAYHOT_ENABLED', False)
    router = payments.create_payment_router(SimpleNamespace(), SimpleNamespace())
    assert any(route.path == settings.PAYHOT_WEBHOOK_PATH and 'POST' in route.methods for route in router.routes)


@pytest.mark.parametrize(
    'event,object_type,status',
    [
        ('recurring_charge.cancelled', 'recurring_charge', 200),
        ('recurring_charge.skipped', 'recurring_charge', 200),
        ('recurring_charge.cancelled', 'payment', 400),
        ('payment.succeeded', 'recurring_charge', 400),
        ('recurring_charge.unrecognized', 'recurring_charge', 400),
    ],
)
async def test_recurring_event_envelope(monkeypatch, event, object_type, status):
    monkeypatch.setattr(
        payhot, 'settings', SimpleNamespace(PAYHOT_WEBHOOK_SECRET=SECRET, PAYHOT_WEBHOOK_PATH='/payhot-webhook')
    )
    processor = AsyncMock(return_value=True)
    monkeypatch.setattr(payments, '_process_payment_service_callback', processor)
    raw = json.dumps(
        {
            'api_version': 'v2',
            'event_id': EVENT_ID,
            'event_type': event,
            'data': {'object': object_type, 'id': EVENT_ID},
        }
    ).encode()
    app = FastAPI()
    payhot.register_payhot_webhook(app.router, object())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/payhot-webhook', content=raw, headers=headers(raw))
    assert response.status_code == status
    assert processor.await_count == int(status == 200)
