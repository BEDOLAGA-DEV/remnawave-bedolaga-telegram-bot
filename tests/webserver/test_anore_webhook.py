"""Raw body authenticity and durable acknowledgement of Anore webhooks."""

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.config import settings
from app.webserver import payments
from app.webserver.anore import register_anore_webhook


def signed(body):
    return {
        'Anore-Signature': hmac.new(b'example', body, hashlib.sha256).hexdigest(),
        'Anore-Event': 'payment.succeeded',
        'Anore-Delivery-Id': 'delivery-1',
    }


@pytest.mark.parametrize(
    'case,code',
    [
        ('ok', 200),
        ('missing', 401),
        ('bad', 401),
        ('changed', 401),
        ('badjson', 400),
        ('array', 400),
        ('nan', 400),
        ('oversize', 413),
        ('event', 400),
        ('delivery', 400),
        ('failed', 500),
        ('exception', 500),
    ],
)
async def test_webhook_contract(monkeypatch, case, code):
    monkeypatch.setattr(settings, 'ANORE_SECRET_KEY', 'example')
    monkeypatch.setattr(settings, 'ANORE_ENABLED', False)
    processor = AsyncMock(return_value=case != 'failed')
    if case == 'exception':
        processor.side_effect = RuntimeError('private provider response')
    monkeypatch.setattr(payments, '_process_payment_service_callback', processor)
    app = FastAPI()
    register_anore_webhook(app, SimpleNamespace())
    body = json.dumps({'event': 'payment.succeeded', '_delivery_id': 'attacker', '_event_header': 'attacker'}).encode()
    if case == 'badjson':
        body = b'{'
    elif case == 'array':
        body = b'[]'
    elif case == 'nan':
        body = b'{"amount":NaN}'
    elif case == 'oversize':
        body = b'x' * 65537
    headers = signed(body)
    if case == 'missing':
        del headers['Anore-Signature']
    elif case == 'bad':
        headers['Anore-Signature'] = 'wrong'
    elif case == 'changed':
        body += b' '
    elif case == 'event':
        headers['Anore-Event'] = 'payment.expired'
    elif case == 'delivery':
        del headers['Anore-Delivery-Id']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/anore-webhook', content=body, headers=headers)
    assert response.status_code == code
    assert 'private' not in response.text
    if case in ('ok', 'failed', 'exception'):
        received = processor.call_args.args[1]
        assert received['_delivery_id'] == 'delivery-1'
        assert received['_event_header'] == 'payment.succeeded'
    else:
        processor.assert_not_awaited()
