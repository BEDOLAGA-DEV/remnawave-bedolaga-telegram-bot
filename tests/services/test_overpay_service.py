from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services.overpay_service import OverpayAPIError, OverpayService


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeClient:
    def __init__(self) -> None:
        self.post_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.post_responses: list[FakeResponse] = []
        self.get_responses: list[FakeResponse] = []

    async def post(self, url: str, json: dict[str, Any] | None = None, headers: dict | None = None) -> FakeResponse:
        self.post_calls.append({'url': url, 'json': json})
        return self.post_responses.pop(0)

    async def get(self, url: str, headers: dict | None = None, timeout: float | None = None) -> FakeResponse:
        self.get_calls.append(url)
        return self.get_responses.pop(0)


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> tuple[OverpayService, FakeClient]:
    monkeypatch.setattr(settings, 'OVERPAY_API_URL', 'https://api-pay.overpay.io', raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_USERNAME', 'login', raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_PASSWORD', 'secret', raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_PROJECT_ID', 'default-project', raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_SERVER_IP', '203.0.113.10', raising=False)
    svc = OverpayService()
    fake = FakeClient()
    monkeypatch.setattr(svc, '_get_client', AsyncMock(return_value=fake))
    return svc, fake


@pytest.mark.asyncio
async def test_create_payment_uses_explicit_project_id(service: tuple[OverpayService, FakeClient]) -> None:
    svc, fake = service
    fake.post_responses = [FakeResponse(201, {'id': 'op-1', 'resultUrl': 'https://pay.overpay.io/form'})]

    result = await svc.create_payment(
        amount='150.00',
        merchant_transaction_id='tx-1',
        project_id='sbp-terminal',
    )

    assert result['resultUrl'] == 'https://pay.overpay.io/form'
    assert fake.post_calls[0]['url'] == 'https://api-pay.overpay.io/orders/'
    assert fake.post_calls[0]['json']['projectId'] == 'sbp-terminal'


@pytest.mark.asyncio
async def test_create_payment_defaults_to_settings_project_id(service: tuple[OverpayService, FakeClient]) -> None:
    svc, fake = service
    fake.post_responses = [FakeResponse(201, {'id': 'op-2', 'resultUrl': 'https://pay'})]

    await svc.create_payment(amount='10.00', merchant_transaction_id='tx-2')

    assert fake.post_calls[0]['json']['projectId'] == 'default-project'


@pytest.mark.asyncio
async def test_create_payment_s2s_payload_contract(service: tuple[OverpayService, FakeClient]) -> None:
    svc, fake = service
    fake.post_responses = [FakeResponse(201, {'id': 'order-9', 'status': 'pending'})]

    result = await svc.create_payment_s2s(
        amount='150.00',
        currency='RUB',
        project_id='sbp-terminal',
        merchant_transaction_id='tx-9',
        client_email='user@example.com',
        return_url='https://t.me/testbot',
    )

    assert result['id'] == 'order-9'
    call = fake.post_calls[0]
    assert call['url'] == 'https://api-pay.overpay.io/api/orders/init'
    assert call['json'] == {
        'paymentMethod': 'fps',
        'type': 'PURCHASE',
        'amount': '150.00',
        'currency': 'RUB',
        'projectId': 'sbp-terminal',
        'merchantTransactionId': 'tx-9',
        'location': {'ip': '203.0.113.10'},
        'client': {'email': 'user@example.com'},
        'options': {'returnUrl': 'https://t.me/testbot'},
    }


@pytest.mark.asyncio
async def test_create_payment_s2s_omits_client_without_email(service: tuple[OverpayService, FakeClient]) -> None:
    svc, fake = service
    fake.post_responses = [FakeResponse(201, {'id': 'order-11', 'status': 'pending'})]

    await svc.create_payment_s2s(
        amount='150.00',
        currency='RUB',
        project_id='sbp-terminal',
        merchant_transaction_id='tx-11',
        return_url='https://t.me/testbot',
    )

    assert 'client' not in fake.post_calls[0]['json']


@pytest.mark.asyncio
async def test_create_payment_s2s_raises_on_error(service: tuple[OverpayService, FakeClient]) -> None:
    svc, fake = service
    fake.post_responses = [FakeResponse(400, {'message': 'bad terminal'})]

    with pytest.raises(OverpayAPIError):
        await svc.create_payment_s2s(
            amount='10.00',
            currency='RUB',
            project_id='sbp-terminal',
            merchant_transaction_id='tx-err',
            client_email='u@t.bot',
            return_url='https://t.me/testbot',
        )


@pytest.mark.asyncio
async def test_wait_for_redirect_link_polls_until_link(service: tuple[OverpayService, FakeClient]) -> None:
    svc, fake = service
    fake.get_responses = [
        FakeResponse(200, {'orders': [{'status': 'pending', 'interaction': {}}]}),
        FakeResponse(
            200, {'orders': [{'status': 'pending', 'interaction': {'redirectLink': 'https://qr.nspk.ru/AS1'}}]}
        ),
    ]

    link = await svc.wait_for_redirect_link('order-1', delay=0)

    assert link == 'https://qr.nspk.ru/AS1'
    assert fake.get_calls == ['https://api-pay.overpay.io/orders/order-1'] * 2


class InvalidJsonResponse(FakeResponse):
    def json(self) -> dict[str, Any]:
        raise ValueError('invalid json')


@pytest.mark.asyncio
async def test_wait_for_redirect_link_survives_invalid_json(service: tuple[OverpayService, FakeClient]) -> None:
    svc, fake = service
    fake.get_responses = [
        InvalidJsonResponse(200),
        FakeResponse(
            200, {'orders': [{'status': 'pending', 'interaction': {'redirectLink': 'https://qr.nspk.ru/AS2'}}]}
        ),
    ]

    link = await svc.wait_for_redirect_link('order-4', delay=0)

    assert link == 'https://qr.nspk.ru/AS2'
    assert len(fake.get_calls) == 2


@pytest.mark.asyncio
async def test_wait_for_redirect_link_stops_on_declined(service: tuple[OverpayService, FakeClient]) -> None:
    svc, fake = service
    fake.get_responses = [FakeResponse(200, {'orders': [{'status': 'declined', 'interaction': {}}]})]

    link = await svc.wait_for_redirect_link('order-2', delay=0)

    assert link is None
    assert len(fake.get_calls) == 1


@pytest.mark.asyncio
async def test_wait_for_redirect_link_gives_up_after_attempts(service: tuple[OverpayService, FakeClient]) -> None:
    svc, fake = service
    fake.get_responses = [FakeResponse(200, {'orders': [{'status': 'processing'}]}) for _ in range(4)]

    link = await svc.wait_for_redirect_link('order-3', delay=0)

    assert link is None
    assert len(fake.get_calls) == 4
