"""Antilopay payment API contract from doc.antilopay.com/AntilopayAPI.pdf."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15

from app.config import settings
from app.services.antilopay_service import AntilopayService


@pytest.fixture
def transport(monkeypatch):
    key = RSA.generate(2048)
    monkeypatch.setattr(settings, 'ANTILOPAY_PRIVATE_KEY', base64.b64encode(key.export_key()).decode())
    monkeypatch.setattr(settings, 'ANTILOPAY_SECRET_ID', 'masked-secret-id')
    monkeypatch.setattr(settings, 'ANTILOPAY_PROJECT_ID', 'masked-project')
    monkeypatch.setattr(settings, 'ANTILOPAY_CURRENCY', 'rub')
    requests = []

    class Response:
        status = 200

        async def json(self, **kwargs):
            return {'code': 0, 'payment_id': 'masked-payment', 'payment_url': 'https://pay.example/invoice'}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    def post(url, *, data, headers):
        # Verify the actual emitted request signature with a generated test key.
        pkcs1_15.new(key.public_key()).verify(SHA256.new(data.encode()), base64.b64decode(headers['X-Apay-Sign']))
        requests.append((url, json.loads(data), headers))
        return Response()

    service = AntilopayService()
    monkeypatch.setattr(service, '_get_session', AsyncMock(return_value=SimpleNamespace(post=post)))
    return service, requests


async def test_create_sends_signed_uppercase_currency(transport):
    service, requests = transport
    result = await service.create_payment(
        amount_rubles=10.0,
        order_id='masked-order',
        product_name='Access',
        description='Subscription',
        customer_email='user@example.com',
        prefer_methods=['SBP'],
        success_url='https://cabinet.example/result',
        fail_url='https://cabinet.example/result',
    )
    _, body, headers = requests[0]
    assert body['currency'] == 'RUB'
    assert body['customer'] == {'email': 'user@example.com'}
    assert body['prefer_methods'] == ['SBP']
    assert headers['X-Apay-Secret-Id'] == 'masked-secret-id'
    assert result['payment_url'] == 'https://pay.example/invoice'
