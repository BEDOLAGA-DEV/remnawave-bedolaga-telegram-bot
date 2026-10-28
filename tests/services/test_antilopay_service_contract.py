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
from app.services.antilopay_service import API_BASE_URL, AntilopayAPIError, AntilopayService


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

        payload = {'code': 0, 'payment_id': 'masked-payment', 'payment_url': 'https://pay.example/invoice'}

        async def json(self, **kwargs):
            return self.payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    def post(url, *, data, headers, **kwargs):
        # Verify the actual emitted request signature with a generated test key.
        pkcs1_15.new(key.public_key()).verify(SHA256.new(data.encode()), base64.b64decode(headers['X-Apay-Sign']))
        requests.append((url, json.loads(data), headers))
        return Response()

    service = AntilopayService()
    service.test_response = Response
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


def test_payment_api_uses_documented_v1():
    assert API_BASE_URL == 'https://lk.antilopay.com/api/v1'


@pytest.mark.parametrize('amount', [True, 0, -1, float('nan'), float('inf'), 1.001])
async def test_invalid_amount_never_creates_invoice(transport, amount):
    service, requests = transport
    with pytest.raises(ValueError):
        await service.create_payment(
            amount_rubles=amount, order_id='masked-order', product_name='Access', customer_email='user@example.com'
        )
    assert not requests


async def test_customer_contact_is_not_fabricated(transport):
    service, requests = transport
    with pytest.raises(ValueError):
        await service.create_payment(amount_rubles=10, order_id='masked-order', product_name='Access')
    assert not requests


@pytest.mark.parametrize(
    'response',
    [[], {'code': 8}, {'code': 0}, {'code': 0, 'payment_id': 'masked', 'payment_url': 'javascript:alert(1)'}],
)
async def test_invalid_create_response_rejected(transport, response):
    service, requests = transport
    service.test_response.payload = response
    with pytest.raises(AntilopayAPIError):
        await service.create_payment(
            amount_rubles=10, order_id='masked-order', product_name='Access', customer_email='user@example.com'
        )
    assert len(requests) == 1


async def test_status_nonzero_code_is_not_success(transport):
    service, requests = transport
    service.test_response.payload = {'code': 8, 'message': 'Not found'}
    with pytest.raises(AntilopayAPIError):
        await service.check_payment(order_id='masked-order')
    assert requests[0][0].endswith('/api/v1/payment/check')
