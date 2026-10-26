"""Current CisPay webhook envelope, distinct from subscription lifecycle events."""

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


@pytest.mark.parametrize(
    'case,expected',
    [
        ('payment', 200),
        ('subscription', 200),
        ('missing_header', 400),
        ('wrong_object', 400),
        ('array', 400),
        ('badjson', 400),
        ('oversize', 413),
        ('badsignature', 400),
        ('failure', 500),
    ],
)
async def test_current_webhook_envelope(monkeypatch, case, expected):
    monkeypatch.setattr(settings, 'CISPAY_SHOP_ID', 'shop-example')
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'synthetic-secret')
    monkeypatch.setattr(settings, 'CISPAY_ENABLED', False)
    processor = AsyncMock(return_value=case != 'failure')
    monkeypatch.setattr(payments, '_process_payment_service_callback', processor)
    app = FastAPI()
    app.include_router(payments.create_payment_router(SimpleNamespace(), SimpleNamespace()))
    data = {'id': 'invoice-1', 'order_id': 'order-1', 'status': 'PAID', 'amount': 10000, 'is_sandbox': False}
    headers = {}
    if case in ('subscription', 'missing_header', 'wrong_object'):
        data = {'event': 'subscription.cancelled', 'object': 'subscription', 'id': 'subscription-1'}
        if case != 'missing_header':
            headers['X-CISPay-Event'] = data['event']
        if case == 'wrong_object':
            data['object'] = 'payment'
    body = json.dumps(data).encode()
    if case == 'array':
        body = b'[]'
    if case == 'badjson':
        body = b'{'
    if case == 'oversize':
        body = b'x' * 65537
    headers['X-Signature'] = hmac.new(b'synthetic-secret', body, hashlib.sha256).hexdigest()
    if case == 'badsignature':
        headers['X-Signature'] = 'invalid'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(settings.CISPAY_WEBHOOK_PATH, content=body, headers=headers)
    assert response.status_code == expected
    if expected in (200, 500):
        processor.assert_awaited_once()
        if case == 'subscription':
            assert processor.call_args.args[1]['_event_header'] == 'subscription.cancelled'
    else:
        processor.assert_not_awaited()
