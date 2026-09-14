"""Клиент TabPay: форма запроса к API и разбор ответов.

Тесты миксина работают на заглушке клиента, поэтому САМО тело запроса нигде не
проверялось: опечатка в имени поля («orderId» -> «order_id») вскрылась бы только
на первом боевом платеже. Здесь закреплены точные имена полей, путь, заголовки
и разбор ошибок по документации провайдера.
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
from app.services.tabpay_service import TabPayAPIError, TabPayNetworkError, TabPayService


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture(autouse=True)
def _creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'TABPAY_API_KEY', 'tp_live_key', raising=False)
    monkeypatch.setattr(settings, 'TABPAY_WEBHOOK_SECRET', 'whsec', raising=False)
    monkeypatch.setattr(settings, 'TABPAY_BASE_URL', 'https://tabpay.org/api', raising=False)


class RecordingService(TabPayService):
    """Клиент с перехваченным транспортом: запоминает, что ушло бы в сеть."""

    def __init__(self, response: Any = None) -> None:
        super().__init__()
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append({'method': method, 'path': path, **kwargs})
        return self.response


def _ok_invoice() -> dict[str, Any]:
    return {
        'id': '6b9d2c88-4b1a-4f0e-9c37-1f2ab34cd561',
        'orderId': 'order-1001',
        'status': 'CREATED',
        'amountKopecks': 19900,
        'payUrl': 'https://tabpay.org/pay/6b9d2c88',
    }


# ---------------------------------------------------------------------------
# Форма запроса на создание платежа
# ---------------------------------------------------------------------------


@pytest.mark.anyio('asyncio')
async def test_create_payment_request_shape() -> None:
    service = RecordingService(_ok_invoice())

    await service.create_payment(
        order_id='order-1001',
        amount_kopecks=19900,
        description='Подписка на месяц',
        email='buyer@example.com',
        telegram_id=987654321,
        metadata={'productId': 42},
        success_url='https://shop.example.com/ok',
        fail_url='https://shop.example.com/fail',
        method='SBP',
    )

    call = service.calls[0]
    assert call['method'] == 'POST'
    assert call['path'] == '/v1/payments'
    assert call['json_payload'] == {
        'orderId': 'order-1001',
        'amountKopecks': 19900,
        'description': 'Подписка на месяц',
        'email': 'buyer@example.com',
        'telegramId': '987654321',  # спека: принимается числом или строкой цифр
        'metadata': {'productId': 42},
        'successUrl': 'https://shop.example.com/ok',
        'failUrl': 'https://shop.example.com/fail',
        'method': 'SBP',
    }


@pytest.mark.anyio('asyncio')
async def test_create_payment_omits_empty_optionals() -> None:
    """Необязательные поля не должны уходить как null — спека их просто не ждёт."""
    service = RecordingService(_ok_invoice())

    await service.create_payment(order_id='order-1', amount_kopecks=19900)

    assert service.calls[0]['json_payload'] == {'orderId': 'order-1', 'amountKopecks': 19900}


@pytest.mark.anyio('asyncio')
async def test_create_payment_truncates_to_api_limits() -> None:
    """orderId 1-64 символа, description до 255 — обрезаем на своей стороне."""
    service = RecordingService(_ok_invoice())

    await service.create_payment(order_id='o' * 100, amount_kopecks=19900, description='d' * 400)

    payload = service.calls[0]['json_payload']
    assert len(payload['orderId']) == 64
    assert len(payload['description']) == 255


@pytest.mark.anyio('asyncio')
async def test_create_payment_rejects_response_without_pay_url() -> None:
    """Без payUrl платёж бесполезен: покупателя некуда вести."""
    service = RecordingService({'id': 'x', 'orderId': 'order-1'})

    with pytest.raises(TabPayAPIError):
        await service.create_payment(order_id='order-1', amount_kopecks=19900)


@pytest.mark.anyio('asyncio')
async def test_create_payment_rejects_response_without_id() -> None:
    service = RecordingService({'payUrl': 'https://tabpay.org/pay/x'})

    with pytest.raises(TabPayAPIError):
        await service.create_payment(order_id='order-1', amount_kopecks=19900)


# ---------------------------------------------------------------------------
# Чтение платежа
# ---------------------------------------------------------------------------


@pytest.mark.anyio('asyncio')
async def test_get_payment_by_id_and_by_order_id() -> None:
    service = RecordingService(_ok_invoice())

    await service.get_payment('6b9d2c88')
    await service.get_payment_by_order_id('order-1001')

    by_id, by_order = service.calls
    assert by_id['method'] == 'GET' and by_id['path'] == '/v1/payments/6b9d2c88'
    assert by_id['allow_404'] is True
    assert by_order['path'] == '/v1/payments'
    assert by_order['params'] == {'orderId': 'order-1001'}
    assert by_order['allow_404'] is True


# ---------------------------------------------------------------------------
# Заголовки и разбор ошибок
# ---------------------------------------------------------------------------


def test_headers_carry_api_key() -> None:
    service = TabPayService()
    headers = service._headers()

    assert headers['X-Api-Key'] == 'tp_live_key'
    assert headers['Content-Type'] == 'application/json'
    # Секрет подписи вебхуков в запросы уходить не должен
    assert 'whsec' not in str(headers)


def test_base_url_falls_back_and_strips_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'TABPAY_BASE_URL', 'https://tabpay.org/api/', raising=False)
    assert TabPayService().base_url == 'https://tabpay.org/api'

    monkeypatch.setattr(settings, 'TABPAY_BASE_URL', '', raising=False)
    assert TabPayService().base_url == 'https://tabpay.org/api'


@pytest.mark.parametrize(
    ('data', 'expected'),
    [
        ({'message': 'Order id is not unique'}, 'Order id is not unique'),
        (
            {'message': ['amountKopecks must not be less than 100', 'orderId should not be empty']},
            'amountKopecks must not be less than 100; orderId should not be empty',
        ),
        ({'error': 'Conflict'}, 'Conflict'),
        ('plain text', 'plain text'),
    ],
)
def test_error_message_formats(data: Any, expected: str) -> None:
    """При ошибках валидации message — массив со всеми проблемами сразу."""
    assert TabPayService._error_message(data) == expected


# ---------------------------------------------------------------------------
# Транспорт: сопоставление HTTP-кодов с исключениями
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


def _service_with(session: _FakeSession) -> TabPayService:
    service = TabPayService()
    service._session = session  # type: ignore[assignment]
    return service


@pytest.mark.anyio('asyncio')
async def test_request_returns_none_on_404_when_allowed() -> None:
    service = _service_with(_FakeSession(_FakeResponse(404, {'message': 'Not found'})))

    assert await service._request('GET', '/v1/payments', allow_404=True) is None


@pytest.mark.anyio('asyncio')
async def test_request_raises_on_404_when_not_allowed() -> None:
    service = _service_with(_FakeSession(_FakeResponse(404, {'message': 'Not found'})))

    with pytest.raises(TabPayAPIError) as exc:
        await service._request('GET', '/v1/payments/x')
    assert exc.value.status_code == 404


@pytest.mark.anyio('asyncio')
async def test_request_raises_api_error_with_message() -> None:
    service = _service_with(_FakeSession(_FakeResponse(409, {'message': 'Order id is not unique'})))

    with pytest.raises(TabPayAPIError) as exc:
        await service._request('POST', '/v1/payments')
    assert exc.value.status_code == 409
    assert 'not unique' in exc.value.message


@pytest.mark.anyio('asyncio')
async def test_connection_error_becomes_network_error() -> None:
    """Исход неизвестен — вызывающий обязан отличать это от отказа API."""
    service = _service_with(_FakeSession(error=aiohttp.ClientError('boom')))

    with pytest.raises(TabPayNetworkError):
        await service._request('POST', '/v1/payments')


@pytest.mark.anyio('asyncio')
async def test_timeout_becomes_network_error() -> None:
    service = _service_with(_FakeSession(error=TimeoutError()))

    with pytest.raises(TabPayNetworkError):
        await service._request('POST', '/v1/payments')
