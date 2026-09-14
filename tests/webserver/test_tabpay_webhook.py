"""Тесты вебхук-эндпоинта TabPay.

Пинят три требования интеграции разом: подпись X-Signature-V2 считается по
СЫРЫМ байтам тела вместе с меткой времени, ответ 200 отдаётся сразу (обработка
уходит в фон, чтобы уложиться в 5 секунд), и товар не выдаётся, пока подпись
не сошлась.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.config import settings
from app.webserver.payments import create_payment_router


SECRET = 'whsec_test_secret'
WEBHOOK_PATH = '/tabpay-webhook'


class DummyBot:
    pass


@pytest.fixture(autouse=True)
def tabpay_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'TABPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'TABPAY_API_KEY', 'tp_test_key', raising=False)
    monkeypatch.setattr(settings, 'TABPAY_WEBHOOK_SECRET', SECRET, raising=False)
    monkeypatch.setattr(settings, 'TABPAY_WEBHOOK_PATH', WEBHOOK_PATH, raising=False)
    monkeypatch.setattr(settings, 'TABPAY_WEBHOOK_MAX_AGE_SECONDS', 300, raising=False)


def _get_route(router, path: str, method: str = 'POST'):
    for route in router.routes:
        if getattr(route, 'path', '') == path and method in getattr(route, 'methods', set()):
            return route
    raise AssertionError(f'Route {path} with method {method} not found')


def _build_request(body: bytes, headers: dict[str, str]) -> Request:
    scope = {
        'type': 'http',
        'asgi': {'version': '3.0'},
        'method': 'POST',
        'path': WEBHOOK_PATH,
        'headers': [(k.lower().encode('latin-1'), v.encode('latin-1')) for k, v in headers.items()],
        'client': ('127.0.0.1', 12345),
    }

    async def receive() -> dict:
        return {'type': 'http.request', 'body': body, 'more_body': False}

    return Request(scope, receive)


def _sign(timestamp: str, raw_body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b'.' + raw_body, hashlib.sha256).hexdigest()


def _signed_request(payload: dict, *, secret: str = SECRET, timestamp: str | None = None) -> Request:
    raw_body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    ts = timestamp if timestamp is not None else str(int(time.time()))
    return _build_request(
        raw_body,
        headers={
            'Content-Type': 'application/json',
            'X-Timestamp': ts,
            'X-Signature-V2': _sign(ts, raw_body, secret),
        },
    )


def _payload(status: str = 'SUCCESS') -> dict:
    return {
        'id': '6b9d2c88-4b1a-4f0e-9c37-1f2ab34cd561',
        'orderId': 'tp123_abcdef12',
        'status': status,
        'amountKopecks': 19900,
        'telegramId': '987654321',
        'metadata': {'productId': 42},
        'test': False,
    }


async def _drain() -> None:
    """Даёт фоновой задаче обработчика доработать до проверок."""
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.anyio
async def test_valid_signature_acks_200_and_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    callback_mock = AsyncMock(return_value=True)
    monkeypatch.setattr('app.webserver.payments._process_payment_service_callback', callback_mock)

    service = SimpleNamespace(process_tabpay_callback=AsyncMock(return_value=True))
    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, WEBHOOK_PATH)
    payload = _payload()
    response = await route.endpoint(_signed_request(payload))
    await _drain()

    assert response.status_code == 200
    assert json.loads(response.body.decode('utf-8'))['status'] == 'ok'
    callback_mock.assert_awaited_once()
    assert callback_mock.await_args.args[1] == payload
    assert callback_mock.await_args.args[2] == 'process_tabpay_callback'


@pytest.mark.anyio
async def test_response_does_not_wait_for_slow_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Обработка уходит в фон: 200 отдаётся, не дожидаясь зачисления."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_callback(*_args, **_kwargs) -> bool:
        started.set()
        await release.wait()
        return True

    monkeypatch.setattr('app.webserver.payments._process_payment_service_callback', slow_callback)

    router = create_payment_router(DummyBot(), SimpleNamespace())
    route = _get_route(router, WEBHOOK_PATH)

    response = await asyncio.wait_for(route.endpoint(_signed_request(_payload())), timeout=1)

    assert response.status_code == 200
    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()
    await _drain()


@pytest.mark.anyio
async def test_invalid_signature_is_rejected_without_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Товар не выдаётся: обработчик не вызывается вовсе."""
    callback_mock = AsyncMock(return_value=True)
    monkeypatch.setattr('app.webserver.payments._process_payment_service_callback', callback_mock)

    router = create_payment_router(DummyBot(), SimpleNamespace())
    route = _get_route(router, WEBHOOK_PATH)

    response = await route.endpoint(_signed_request(_payload(), secret='wrong-secret'))
    await _drain()

    assert response.status_code == 400
    callback_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_stale_timestamp_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Перехваченный вебхук нельзя переиграть позже: метка вне окна."""
    callback_mock = AsyncMock(return_value=True)
    monkeypatch.setattr('app.webserver.payments._process_payment_service_callback', callback_mock)

    router = create_payment_router(DummyBot(), SimpleNamespace())
    route = _get_route(router, WEBHOOK_PATH)

    stale = str(int(time.time()) - 600)
    response = await route.endpoint(_signed_request(_payload(), timestamp=stale))
    await _drain()

    assert response.status_code == 400
    callback_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_reserialized_body_breaks_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подпись обязана считаться по сырым байтам: пробелы меняют результат."""
    callback_mock = AsyncMock(return_value=True)
    monkeypatch.setattr('app.webserver.payments._process_payment_service_callback', callback_mock)

    router = create_payment_router(DummyBot(), SimpleNamespace())
    route = _get_route(router, WEBHOOK_PATH)

    payload = _payload()
    signed_bytes = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    reserialized = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
    ts = str(int(time.time()))

    response = await route.endpoint(
        _build_request(
            reserialized,
            headers={'X-Timestamp': ts, 'X-Signature-V2': _sign(ts, signed_bytes)},
        )
    )
    await _drain()

    assert response.status_code == 400
    callback_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_legacy_v1_signature_is_not_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Принимаем только v2: подпись от одного тела не проходит."""
    callback_mock = AsyncMock(return_value=True)
    monkeypatch.setattr('app.webserver.payments._process_payment_service_callback', callback_mock)

    router = create_payment_router(DummyBot(), SimpleNamespace())
    route = _get_route(router, WEBHOOK_PATH)

    payload = _payload()
    raw_body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    ts = str(int(time.time()))
    legacy = hmac.new(SECRET.encode(), raw_body, hashlib.sha256).hexdigest()

    response = await route.endpoint(_build_request(raw_body, headers={'X-Timestamp': ts, 'X-Signature': legacy}))
    await _drain()

    assert response.status_code == 400
    callback_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_broken_json_with_valid_signature_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    callback_mock = AsyncMock(return_value=True)
    monkeypatch.setattr('app.webserver.payments._process_payment_service_callback', callback_mock)

    router = create_payment_router(DummyBot(), SimpleNamespace())
    route = _get_route(router, WEBHOOK_PATH)

    raw_body = b'{"id": broken'
    ts = str(int(time.time()))
    response = await route.endpoint(
        _build_request(raw_body, headers={'X-Timestamp': ts, 'X-Signature-V2': _sign(ts, raw_body)})
    )
    await _drain()

    assert response.status_code == 400
    callback_mock.assert_not_awaited()


def _mounted_paths(router) -> set[str]:
    return {getattr(route, 'path', '') for route in getattr(router, 'routes', []) or []}


@pytest.mark.anyio
async def test_route_absent_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ненастроенный провайдер не должен держать открытый эндпоинт."""
    assert WEBHOOK_PATH in _mounted_paths(create_payment_router(DummyBot(), SimpleNamespace()))

    monkeypatch.setattr(settings, 'TABPAY_WEBHOOK_SECRET', None, raising=False)
    assert WEBHOOK_PATH not in _mounted_paths(create_payment_router(DummyBot(), SimpleNamespace()))
