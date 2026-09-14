"""Эндпоинт HTTP-уведомлений ParityPay.

Пинит три требования: подпись X-SIGNATURE проверяется до всякой обработки,
200 отдаётся сразу (иначе провайдер повторит доставку пять раз), и без валидной
подписи обработчик не вызывается вовсе.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.config import settings
from app.services.paritypay_service import ParityPayService
from app.webserver.payments import create_payment_router


SECRET = 'callback-secret-key-2'
WEBHOOK_PATH = '/paritypay-webhook'


class DummyBot:
    pass


@pytest.fixture(autouse=True)
def paritypay_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'PARITYPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'PARITYPAY_SHOP_ID', '874dfb1e-shop', raising=False)
    monkeypatch.setattr(settings, 'PARITYPAY_SECRET_KEY', 'secret-1', raising=False)
    monkeypatch.setattr(settings, 'PARITYPAY_CALLBACK_SECRET', SECRET, raising=False)
    monkeypatch.setattr(settings, 'PARITYPAY_WEBHOOK_PATH', WEBHOOK_PATH, raising=False)


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


PAYLOAD = {
    'id': '9beea835-0937-4b5c-8f5a-c3a0d0e60346',
    'order_id': 'pp123_abcdef12',
    'shop_id': '874dfb1e-dbdb-4747-a1c0-005969725b74',
    'amount': '1250.00',
    'credited': 1209.01,
    'comment': None,
    'service': 'sbp',
    'custom_fields': None,
    'expires': '2026-08-24 13:40:00',
    'created': '2026-08-24 12:40:00',
    'status': 'PAID',
}


def _signed_request(payload: dict | None = None, *, secret: str = SECRET) -> Request:
    body = json.dumps(payload if payload is not None else PAYLOAD, ensure_ascii=False).encode('utf-8')
    parsed = ParityPayService.parse_callback_body(body)
    signature_payload = ParityPayService.build_signature_payload(parsed)
    signature = hmac.new(secret.encode(), signature_payload.encode(), hashlib.sha256).hexdigest()
    return _build_request(body, {'Content-Type': 'application/json', 'X-SIGNATURE': signature})


async def _drain() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.anyio
async def test_valid_signature_acks_and_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    callback_mock = AsyncMock(return_value=True)
    monkeypatch.setattr('app.webserver.payments._process_payment_service_callback', callback_mock)

    router = create_payment_router(DummyBot(), SimpleNamespace())
    assert router is not None

    response = await _get_route(router, WEBHOOK_PATH).endpoint(_signed_request())
    await _drain()

    assert response.status_code == 200
    callback_mock.assert_awaited_once()
    assert callback_mock.await_args.args[2] == 'process_paritypay_callback'
    # В обработчик уходит тело с СОХРАНЁННЫМ текстом чисел
    assert callback_mock.await_args.args[1]['amount'] == '1250.00'
    assert callback_mock.await_args.args[1]['credited'] == '1209.01'


@pytest.mark.anyio
async def test_response_does_not_wait_for_slow_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(*_args, **_kwargs) -> bool:
        started.set()
        await release.wait()
        return True

    monkeypatch.setattr('app.webserver.payments._process_payment_service_callback', slow)

    router = create_payment_router(DummyBot(), SimpleNamespace())
    response = await asyncio.wait_for(_get_route(router, WEBHOOK_PATH).endpoint(_signed_request()), timeout=1)

    assert response.status_code == 200
    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()
    await _drain()


@pytest.mark.anyio
async def test_invalid_signature_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    callback_mock = AsyncMock(return_value=True)
    monkeypatch.setattr('app.webserver.payments._process_payment_service_callback', callback_mock)

    router = create_payment_router(DummyBot(), SimpleNamespace())
    response = await _get_route(router, WEBHOOK_PATH).endpoint(_signed_request(secret='wrong'))
    await _drain()

    assert response.status_code == 400
    callback_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_tampered_amount_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подмена суммы после подписи ломает проверку."""
    callback_mock = AsyncMock(return_value=True)
    monkeypatch.setattr('app.webserver.payments._process_payment_service_callback', callback_mock)

    router = create_payment_router(DummyBot(), SimpleNamespace())

    body = json.dumps(PAYLOAD, ensure_ascii=False).encode('utf-8')
    parsed = ParityPayService.parse_callback_body(body)
    signature = hmac.new(
        SECRET.encode(), ParityPayService.build_signature_payload(parsed).encode(), hashlib.sha256
    ).hexdigest()
    tampered = json.dumps({**PAYLOAD, 'amount': '99999.00'}, ensure_ascii=False).encode('utf-8')

    response = await _get_route(router, WEBHOOK_PATH).endpoint(_build_request(tampered, {'X-SIGNATURE': signature}))
    await _drain()

    assert response.status_code == 400
    callback_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_missing_signature_header_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    callback_mock = AsyncMock(return_value=True)
    monkeypatch.setattr('app.webserver.payments._process_payment_service_callback', callback_mock)

    router = create_payment_router(DummyBot(), SimpleNamespace())
    body = json.dumps(PAYLOAD, ensure_ascii=False).encode('utf-8')

    response = await _get_route(router, WEBHOOK_PATH).endpoint(_build_request(body, {}))
    await _drain()

    assert response.status_code == 400
    callback_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_broken_json_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    callback_mock = AsyncMock(return_value=True)
    monkeypatch.setattr('app.webserver.payments._process_payment_service_callback', callback_mock)

    router = create_payment_router(DummyBot(), SimpleNamespace())
    response = await _get_route(router, WEBHOOK_PATH).endpoint(
        _build_request(b'{"id": broken', {'X-SIGNATURE': 'x' * 64})
    )
    await _drain()

    assert response.status_code == 400
    callback_mock.assert_not_awaited()


def _mounted_paths(router) -> set[str]:
    return {getattr(route, 'path', '') for route in getattr(router, 'routes', []) or []}


@pytest.mark.anyio
async def test_route_absent_without_callback_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без ключа подписи эндпоинт не монтируется — принимать вслепую нечего."""
    assert WEBHOOK_PATH in _mounted_paths(create_payment_router(DummyBot(), SimpleNamespace()))

    monkeypatch.setattr(settings, 'PARITYPAY_CALLBACK_SECRET', None, raising=False)
    assert WEBHOOK_PATH not in _mounted_paths(create_payment_router(DummyBot(), SimpleNamespace()))
