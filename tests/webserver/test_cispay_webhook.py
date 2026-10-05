"""Regression tests for cisPay sandbox webhook handling."""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.config import settings
from app.webserver import payments
from app.webserver.payments import create_payment_router


API_KEY = 'cis_sec_test'


class DummyBot:
    pass


def _sign(body: bytes) -> str:
    return hmac.new(API_KEY.encode('utf-8'), body, hashlib.sha256).hexdigest()


def _build_request(payload: object, *, signature: str | None = None) -> Request:
    body = json.dumps(payload).encode('utf-8')
    headers: list[tuple[bytes, bytes]] = []
    if signature is not None:
        headers.append((b'x-signature', signature.encode('latin-1')))

    scope = {
        'type': 'http',
        'asgi': {'version': '3.0'},
        'method': 'POST',
        'path': '/cispay-webhook',
        'headers': headers,
        'client': ('127.0.0.1', 12345),
    }

    async def receive() -> dict:
        return {'type': 'http.request', 'body': body, 'more_body': False}

    return Request(scope, receive)


def _get_cispay_route(router):
    for route in router.routes:
        if getattr(route, 'path', '') == '/cispay-webhook' and 'POST' in getattr(route, 'methods', set()):
            return route
    raise AssertionError('cisPay webhook route not found')


@pytest.fixture(autouse=True)
def cispay_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CISPAY_SHOP_ID', 'shop', raising=False)
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', API_KEY, raising=False)
    monkeypatch.setattr(settings, 'CISPAY_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'CISPAY_WEBHOOK_PATH', '/cispay-webhook', raising=False)


@pytest.mark.anyio
async def test_signed_sandbox_webhook_returns_200_without_production_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback = AsyncMock(return_value=True)
    monkeypatch.setattr(payments, '_process_payment_service_callback', callback)

    payload = {
        'id': 'sandbox-payment',
        'order_id': 'sandbox-order',
        'is_sandbox': True,
        'status': 'PAID',
        'amount': 10000,
        'charged_amount': 10400,
    }
    body = json.dumps(payload).encode('utf-8')
    route = _get_cispay_route(create_payment_router(DummyBot(), SimpleNamespace()))

    response = await route.endpoint(_build_request(payload, signature=_sign(body)))

    assert response.status_code == 200
    assert json.loads(response.body.decode('utf-8')) == {'status': 'ok'}
    callback.assert_not_awaited()


@pytest.mark.anyio
async def test_sandbox_webhook_requires_valid_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    callback = AsyncMock(return_value=True)
    monkeypatch.setattr(payments, '_process_payment_service_callback', callback)

    payload = {
        'id': 'sandbox-payment',
        'order_id': 'sandbox-order',
        'is_sandbox': True,
        'status': 'PAID',
    }
    route = _get_cispay_route(create_payment_router(DummyBot(), SimpleNamespace()))

    response = await route.endpoint(_build_request(payload, signature='invalid'))

    assert response.status_code == 400
    callback.assert_not_awaited()


@pytest.mark.anyio
async def test_string_sandbox_flag_stays_on_production_callback_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback = AsyncMock(return_value=True)
    monkeypatch.setattr(payments, '_process_payment_service_callback', callback)

    payload = {
        'id': 'payment',
        'order_id': 'order',
        'is_sandbox': 'true',
        'status': 'PAID',
    }
    body = json.dumps(payload).encode('utf-8')
    payment_service = SimpleNamespace()
    route = _get_cispay_route(create_payment_router(DummyBot(), payment_service))

    response = await route.endpoint(_build_request(payload, signature=_sign(body)))

    assert response.status_code == 200
    callback.assert_awaited_once_with(payment_service, payload, 'process_cispay_callback')
