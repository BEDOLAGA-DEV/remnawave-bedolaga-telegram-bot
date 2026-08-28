"""Юнит-тесты MulenPayService."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Self

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings
from app.services.mulenpay_service import MULENPAY_CLIENT_MAX_LENGTH, MulenPayService


class _DummyResponse:
    def __init__(
        self,
        *,
        status: int,
        body: str = '{}',
        headers: dict[str, str] | None = None,
        url: str = 'https://mulenpay.test/endpoint',
    ) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {'Content-Type': 'application/json'}
        self.url = url

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # pragma: no cover - interface
        return False

    async def text(self) -> str:
        return self._body


class _DummySession:
    def __init__(self, result: Any) -> None:
        self._result = result

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # pragma: no cover - interface
        return False

    def request(self, *args: Any, **kwargs: Any) -> Any:
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


def _session_factory(responses: Sequence[Any]) -> Any:
    call_state = {'index': 0}

    def _factory(*_args: Any, **_kwargs: Any) -> _DummySession:
        index = min(call_state['index'], len(responses) - 1)
        call_state['index'] += 1
        return _DummySession(responses[index])

    return _factory


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


def _enable_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(settings), 'is_mulenpay_enabled', lambda self: True, raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_API_KEY', 'api', raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_SHOP_ID', 'shop', raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_SECRET_KEY', 'secret', raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_BASE_URL', 'https://mulenpay.test', raising=False)


def test_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    service = MulenPayService()
    assert service.is_configured is False

    _enable_service(monkeypatch)
    service = MulenPayService()
    assert service.is_configured is True


def test_format_and_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_service(monkeypatch)
    service = MulenPayService()
    assert service._format_amount(12345) == '123.45'
    signature = service._build_signature('rub', '100.00')
    assert isinstance(signature, str) and len(signature) == 40


@pytest.mark.anyio('asyncio')
async def test_create_payment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_service(monkeypatch)

    captured_payload: dict[str, Any] = {}

    async def fake_request(method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        captured_payload.update({'method': method, 'endpoint': endpoint, **kwargs})
        return {'success': True, 'id': 101, 'paymentUrl': 'https://mulenpay/pay'}

    service = MulenPayService()
    monkeypatch.setattr(service, '_request', fake_request, raising=False)

    result = await service.create_payment(
        amount_kopeks=25000,
        description='Пополнение',
        uuid='uuid-1',
        items=[{'description': 'item', 'quantity': 1, 'price': 250.0}],
        language='ru',
        website_url='https://example.com',
    )

    assert result is not None
    assert result['id'] == 101
    assert captured_payload['method'] == 'POST'
    assert captured_payload['endpoint'] == '/v2/payments'
    assert captured_payload['json_data']['language'] == 'ru'
    assert 'client' not in captured_payload['json_data']


@pytest.mark.anyio('asyncio')
async def test_create_payment_includes_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_service(monkeypatch)

    captured_payload: dict[str, Any] = {}

    async def fake_request(method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        captured_payload.update({'method': method, 'endpoint': endpoint, **kwargs})
        return {'success': True, 'id': 101, 'paymentUrl': 'https://mulenpay/pay'}

    service = MulenPayService()
    monkeypatch.setattr(service, '_request', fake_request, raising=False)

    await service.create_payment(
        amount_kopeks=25000,
        description='Пополнение',
        uuid='uuid-1',
        items=[{'description': 'item', 'quantity': 1, 'price': 250.0}],
        client='user@example.com',
    )

    assert captured_payload['json_data']['client'] == 'user@example.com'


@pytest.mark.anyio('asyncio')
@pytest.mark.parametrize('client', ['', None])
async def test_create_payment_omits_empty_client(monkeypatch: pytest.MonkeyPatch, client: str | None) -> None:
    """Пустое значение не должно уходить в payload как пустая строка."""
    _enable_service(monkeypatch)

    captured_payload: dict[str, Any] = {}

    async def fake_request(method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        captured_payload.update({'method': method, 'endpoint': endpoint, **kwargs})
        return {'success': True, 'id': 101, 'paymentUrl': 'https://mulenpay/pay'}

    service = MulenPayService()
    monkeypatch.setattr(service, '_request', fake_request, raising=False)

    await service.create_payment(
        amount_kopeks=25000,
        description='Пополнение',
        uuid='uuid-1',
        items=[{'description': 'item', 'quantity': 1, 'price': 250.0}],
        client=client,
    )

    assert 'client' not in captured_payload['json_data']


@pytest.mark.anyio('asyncio')
async def test_create_payment_truncates_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_service(monkeypatch)

    captured_payload: dict[str, Any] = {}

    async def fake_request(method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        captured_payload.update({'method': method, 'endpoint': endpoint, **kwargs})
        return {'success': True, 'id': 101, 'paymentUrl': 'https://mulenpay/pay'}

    service = MulenPayService()
    monkeypatch.setattr(service, '_request', fake_request, raising=False)

    await service.create_payment(
        amount_kopeks=25000,
        description='Пополнение',
        uuid='uuid-1',
        items=[{'description': 'item', 'quantity': 1, 'price': 250.0}],
        client='x' * 400,
    )

    assert len(captured_payload['json_data']['client']) == MULENPAY_CLIENT_MAX_LENGTH


@pytest.mark.anyio('asyncio')
async def test_create_payment_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_service(monkeypatch)
    service = MulenPayService()

    async def fake_request(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return None

    monkeypatch.setattr(service, '_request', fake_request, raising=False)

    result = await service.create_payment(
        amount_kopeks=1000,
        description='desc',
        uuid='uuid',
        items=[],
    )
    assert result is None


@pytest.mark.anyio('asyncio')
async def test_get_payment(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_service(monkeypatch)
    service = MulenPayService()

    async def fake_request(method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        return {'id': 123, 'status': 'paid'}

    monkeypatch.setattr(service, '_request', fake_request, raising=False)
    result = await service.get_payment(123)
    assert result == {'id': 123, 'status': 'paid'}


@pytest.mark.anyio('asyncio')
async def test_request_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_service(monkeypatch)
    service = MulenPayService()

    response_payload = {'ok': True}
    monkeypatch.setattr(
        'app.services.mulenpay_service.aiohttp.ClientSession',
        _session_factory(
            [
                _DummyResponse(status=200, body=json.dumps(response_payload)),
            ]
        ),
    )

    result = await service._request('GET', '/ping')
    assert result == response_payload


@pytest.mark.anyio('asyncio')
async def test_request_retries_on_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_service(monkeypatch)
    service = MulenPayService()
    service._max_retries = 2

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(
        'app.services.mulenpay_service.asyncio.sleep',
        fake_sleep,
    )

    monkeypatch.setattr(
        'app.services.mulenpay_service.aiohttp.ClientSession',
        _session_factory(
            [
                _DummyResponse(status=502, body='{"error": "bad gateway"}'),
                _DummyResponse(status=200, body='{"ok": true}'),
            ]
        ),
    )

    result = await service._request('GET', '/retry')
    assert result == {'ok': True}
    assert sleep_calls == [service._retry_delay]


@pytest.mark.anyio('asyncio')
async def test_request_returns_none_after_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_service(monkeypatch)
    service = MulenPayService()
    service._max_retries = 2

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(
        'app.services.mulenpay_service.asyncio.sleep',
        fake_sleep,
    )

    monkeypatch.setattr(
        'app.services.mulenpay_service.aiohttp.ClientSession',
        _session_factory([TimeoutError()]),
    )

    result = await service._request('GET', '/timeout')
    assert result is None


@pytest.mark.anyio('asyncio')
async def test_request_reraises_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_service(monkeypatch)
    service = MulenPayService()

    monkeypatch.setattr(
        'app.services.mulenpay_service.aiohttp.ClientSession',
        _session_factory([asyncio.CancelledError()]),
    )

    with pytest.raises(asyncio.CancelledError):
        await service._request('GET', '/cancel')
