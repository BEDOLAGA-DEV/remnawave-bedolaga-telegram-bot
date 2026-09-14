"""Клиент ParityPay: форма запроса к API и разбор ответов.

Тесты миксина работают на заглушке клиента, поэтому само тело запроса нигде не
проверялось. Здесь закреплены точные имена полей, путь, заголовки и — главное —
что сумма уходит в РУБЛЯХ, а не в копейках: провайдер молча принял бы 125000
рублей вместо 1250.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Self

import aiohttp
import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings
from app.services.paritypay_service import ParityPayAPIError, ParityPayNetworkError, ParityPayService


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture(autouse=True)
def _creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'PARITYPAY_SHOP_ID', '874dfb1e-dbdb-4747-a1c0-005969725b74', raising=False)
    monkeypatch.setattr(settings, 'PARITYPAY_SECRET_KEY', 'secret-key-1', raising=False)
    monkeypatch.setattr(settings, 'PARITYPAY_CALLBACK_SECRET', 'secret-key-2', raising=False)
    monkeypatch.setattr(settings, 'PARITYPAY_BASE_URL', 'https://api.paritypay.net', raising=False)


class RecordingService(ParityPayService):
    def __init__(self, response: Any = None) -> None:
        super().__init__()
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append({'method': method, 'path': path, **kwargs})
        return self.response


def _ok_invoice() -> dict[str, Any]:
    return {
        'id': '9beea835-0937-4b5c-8f5a-c3a0d0e60346',
        'order_id': 'order-1001',
        'amount': 1250.0,
        'status': 'NEW',
        'link': 'https://pay.paritypay.net/9beea835',
    }


# ---------------------------------------------------------------------------
# Форма запроса на создание счёта
# ---------------------------------------------------------------------------


@pytest.mark.anyio('asyncio')
async def test_create_invoice_sends_rubles_not_kopeks() -> None:
    """125000 копеек обязаны уйти как 1250.0 — иначе счёт будет на 125 000 ₽."""
    service = RecordingService(_ok_invoice())

    await service.create_invoice(order_id='order-1001', amount_kopeks=125000)

    payload = service.calls[0]['json_payload']
    assert payload['amount'] == 1250.0
    assert isinstance(payload['amount'], float)


@pytest.mark.anyio('asyncio')
async def test_create_invoice_request_shape() -> None:
    service = RecordingService(_ok_invoice())

    await service.create_invoice(
        order_id='order-1001',
        amount_kopeks=150050,
        comment='Оплата заказа №1001',
        custom_fields='balance',
        service='sbp',
        expire_minutes=60,
        success_url='https://shop.example.com/success',
        fail_url='https://shop.example.com/fail',
        callback_url='https://shop.example.com/callback',
    )

    call = service.calls[0]
    assert call['method'] == 'POST'
    assert call['path'] == '/v2/invoice/create'
    assert call['json_payload'] == {
        'order_id': 'order-1001',
        'amount': 1500.5,
        'comment': 'Оплата заказа №1001',
        'custom_fields': 'balance',
        'service': 'sbp',
        'expire': 60,
        'success_url': 'https://shop.example.com/success',
        'fail_url': 'https://shop.example.com/fail',
        'callback_url': 'https://shop.example.com/callback',
    }


@pytest.mark.anyio('asyncio')
async def test_create_invoice_never_sends_subscription_block() -> None:
    """Подписки не оформляем: блок subscription не должен появляться никогда."""
    service = RecordingService(_ok_invoice())

    await service.create_invoice(order_id='order-1', amount_kopeks=99000, service='sbp')

    assert 'subscription' not in service.calls[0]['json_payload']


@pytest.mark.anyio('asyncio')
async def test_create_invoice_omits_empty_optionals() -> None:
    service = RecordingService(_ok_invoice())

    await service.create_invoice(order_id='order-1', amount_kopeks=99000)

    assert service.calls[0]['json_payload'] == {'order_id': 'order-1', 'amount': 990.0}


@pytest.mark.anyio('asyncio')
async def test_create_invoice_rejects_response_without_link() -> None:
    service = RecordingService({'id': 'x', 'order_id': 'order-1'})

    with pytest.raises(ParityPayAPIError):
        await service.create_invoice(order_id='order-1', amount_kopeks=99000)


@pytest.mark.anyio('asyncio')
async def test_create_invoice_rejects_response_without_id() -> None:
    service = RecordingService({'link': 'https://pay.paritypay.net/x'})

    with pytest.raises(ParityPayAPIError):
        await service.create_invoice(order_id='order-1', amount_kopeks=99000)


# ---------------------------------------------------------------------------
# Чтение счёта
# ---------------------------------------------------------------------------


@pytest.mark.anyio('asyncio')
async def test_get_invoice_by_id_and_by_order_id() -> None:
    service = RecordingService(_ok_invoice())

    await service.get_invoice(invoice_id='9beea835')
    await service.get_invoice(order_id='order-1001')

    by_id, by_order = service.calls
    assert by_id['method'] == 'GET' and by_id['path'] == '/v2/invoice/status'
    assert by_id['params'] == {'id': '9beea835'}
    assert by_id['allow_404'] is True
    assert by_order['params'] == {'order_id': 'order-1001'}


@pytest.mark.anyio('asyncio')
async def test_get_invoice_prefers_id_over_order_id() -> None:
    """Спека: передаётся ОДИН из параметров, не оба."""
    service = RecordingService(_ok_invoice())

    await service.get_invoice(invoice_id='9beea835', order_id='order-1001')

    assert service.calls[0]['params'] == {'id': '9beea835'}


@pytest.mark.anyio('asyncio')
async def test_get_invoice_without_identifiers_raises() -> None:
    service = RecordingService(_ok_invoice())

    with pytest.raises(ValueError, match='invoice_id'):
        await service.get_invoice()


# ---------------------------------------------------------------------------
# Заголовки и ошибки
# ---------------------------------------------------------------------------


def test_headers_carry_shop_and_secret_key() -> None:
    headers = ParityPayService()._headers()

    assert headers['X-ShopId'] == '874dfb1e-dbdb-4747-a1c0-005969725b74'
    assert headers['X-SecretKey'] == 'secret-key-1'
    assert headers['Content-Type'] == 'application/json'
    # Ключ подписи уведомлений (№2) в запросы уходить не должен
    assert 'secret-key-2' not in str(headers)


def test_base_url_strips_slash_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'PARITYPAY_BASE_URL', 'https://api.paritypay.net/', raising=False)
    assert ParityPayService().base_url == 'https://api.paritypay.net'

    monkeypatch.setattr(settings, 'PARITYPAY_BASE_URL', '', raising=False)
    assert ParityPayService().base_url == 'https://api.paritypay.net'


@pytest.mark.parametrize(
    ('data', 'expected'),
    [
        ({'error': 'Order id is not unique'}, 'Order id is not unique'),
        ({'error': "Service 'foo' is not a valid."}, "Service 'foo' is not a valid."),
        ({'error': 'Insufficient balance'}, 'Insufficient balance'),
        ('plain', 'plain'),
    ],
)
def test_error_message_uses_error_field(data: Any, expected: str) -> None:
    """Формат ошибки провайдера — объект {"error": "текст"}."""
    assert ParityPayService._error_message(data) == expected


# ---------------------------------------------------------------------------
# Транспорт
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def json(self, **_kwargs: Any) -> Any:
        return self._payload

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakeSession:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.closed = False

    def request(self, *_args: Any, **_kwargs: Any) -> Any:
        if self._error:
            raise self._error
        return self._response


def _service_with(session: _FakeSession) -> ParityPayService:
    service = ParityPayService()
    service._session = session  # type: ignore[assignment]
    return service


@pytest.mark.anyio('asyncio')
async def test_request_404_allowed_returns_none() -> None:
    service = _service_with(_FakeSession(_FakeResponse(404, {'error': 'Invoice not found'})))

    assert await service._request('GET', '/v2/invoice/status', allow_404=True) is None


@pytest.mark.anyio('asyncio')
async def test_request_422_raises_business_error() -> None:
    service = _service_with(_FakeSession(_FakeResponse(422, {'error': 'Order id is not unique'})))

    with pytest.raises(ParityPayAPIError) as exc:
        await service._request('POST', '/v2/invoice/create')
    assert exc.value.status_code == 422
    assert 'not unique' in exc.value.message


@pytest.mark.anyio('asyncio')
async def test_request_400_raises() -> None:
    service = _service_with(_FakeSession(_FakeResponse(400, {'error': 'Магазин не найден'})))

    with pytest.raises(ParityPayAPIError) as exc:
        await service._request('GET', '/v2/shop/balance')
    assert exc.value.status_code == 400


@pytest.mark.anyio('asyncio')
async def test_connection_error_and_timeout_become_network_error() -> None:
    with pytest.raises(ParityPayNetworkError):
        await _service_with(_FakeSession(error=aiohttp.ClientError('boom')))._request('POST', '/v2/invoice/create')

    with pytest.raises(ParityPayNetworkError):
        await _service_with(_FakeSession(error=TimeoutError()))._request('POST', '/v2/invoice/create')
