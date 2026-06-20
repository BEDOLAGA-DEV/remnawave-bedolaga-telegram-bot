"""Regression tests for MulenPay webhook signature verification.

Background (2026-05-18 incident):
After migration from legacy aiohttp ``WebhookServer`` to unified FastAPI
``create_payment_router`` in 2.5.7, MulenPay webhooks started returning
401 ``invalid_signature`` with logs ``"Отсутствует подпись webhook"``.

Root cause confirmed via MulenPay OpenAPI spec
(https://mulenpay.ru/docs/api/definition?openapi_mulen_pay) and the
official ``mulenpay-api`` Python SDK v1.0.12
(``mulenpay_api/utils/calculus.py``): MulenPay puts the signature in the
**JSON body** as the ``sign`` field, not in any HTTP header.

Formula::

    data_str = ''.join(str(v) for v in data.values())  # excluding 'sign'
    sign = hashlib.sha1((data_str + secret_key).encode()).hexdigest()

These tests pin the new body-level verification flow.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.config import settings
from app.webserver.payments import _verify_mulenpay_signature, create_payment_router


SECRET = 'test-secret-key'


def _sign(data: dict, secret: str = SECRET) -> str:
    """Reproduce official MulenPay SDK formula."""
    data_str = ''.join(str(v) for v in data.values())
    return hashlib.sha1((data_str + secret).encode('utf-8')).hexdigest()


@pytest.fixture(autouse=True)
def mulenpay_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'MULENPAY_SECRET_KEY', SECRET, raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_API_KEY', 'k', raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_SHOP_ID', 1, raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_WEBHOOK_PATH', '/mulen', raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_DISPLAY_NAME', 'MulenPay', raising=False)


def _build_request(body: bytes, headers: dict[str, str] | None = None) -> Request:
    headers = headers or {}
    scope = {
        'type': 'http',
        'asgi': {'version': '3.0'},
        'method': 'POST',
        'path': '/mulen',
        'headers': [(k.lower().encode('latin-1'), v.encode('latin-1')) for k, v in headers.items()],
        'client': ('127.0.0.1', 12345),
    }

    async def receive() -> dict:
        return {'type': 'http.request', 'body': body, 'more_body': False}

    return Request(scope, receive)


def test_verify_accepts_valid_body_sign() -> None:
    data = {
        'id': 123,
        'amount': '100.00',
        'currency': 'rub',
        'uuid': 'mulen_42_abc',
        'payment_status': 'success',
    }
    payload = {**data, 'sign': _sign(data)}
    body = json.dumps(payload).encode('utf-8')

    assert _verify_mulenpay_signature(_build_request(body), body) is True


def test_verify_rejects_wrong_sign() -> None:
    data = {
        'id': 123,
        'amount': '100.00',
        'currency': 'rub',
        'uuid': 'mulen_42_abc',
        'payment_status': 'success',
    }
    payload = {**data, 'sign': 'a' * 40}
    body = json.dumps(payload).encode('utf-8')

    assert _verify_mulenpay_signature(_build_request(body), body) is False


def test_verify_rejects_missing_sign_field() -> None:
    payload = {
        'id': 123,
        'amount': '100.00',
        'currency': 'rub',
        'uuid': 'mulen_42_abc',
        'payment_status': 'success',
    }
    body = json.dumps(payload).encode('utf-8')

    assert _verify_mulenpay_signature(_build_request(body), body) is False


def test_verify_rejects_tampered_amount() -> None:
    data = {
        'id': 123,
        'amount': '100.00',
        'currency': 'rub',
        'uuid': 'mulen_42_abc',
        'payment_status': 'success',
    }
    correct_sign = _sign(data)
    tampered = {**data, 'amount': '99999.00', 'sign': correct_sign}
    body = json.dumps(tampered).encode('utf-8')

    assert _verify_mulenpay_signature(_build_request(body), body) is False


def test_verify_rejects_when_secret_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'MULENPAY_SECRET_KEY', None, raising=False)

    data = {'id': 1, 'amount': '1.00'}
    payload = {**data, 'sign': 'whatever'}
    body = json.dumps(payload).encode('utf-8')

    assert _verify_mulenpay_signature(_build_request(body), body) is False


def test_verify_rejects_non_json_body() -> None:
    body = b'not-json-at-all'

    assert _verify_mulenpay_signature(_build_request(body), body) is False


def test_verify_rejects_json_array_payload() -> None:
    body = json.dumps([1, 2, 3]).encode('utf-8')

    assert _verify_mulenpay_signature(_build_request(body), body) is False


def test_verify_rejects_empty_object() -> None:
    body = json.dumps({}).encode('utf-8')

    assert _verify_mulenpay_signature(_build_request(body), body) is False


def test_verify_ignores_http_headers_completely() -> None:
    """User can no longer trick verification by sending X-Signature.

    Regression: before the fix, header-based extraction made signature
    spoofing trivial when the secret leaked, because Authorization: Bearer
    could replay the secret. New flow only inspects body-level sign field.
    """
    data = {'id': 1, 'amount': '1.00', 'currency': 'rub', 'uuid': 'u', 'payment_status': 'success'}
    payload = {**data, 'sign': 'invalid'}
    body = json.dumps(payload).encode('utf-8')

    request = _build_request(
        body,
        headers={
            'X-Signature': 'should-not-help',
            'X-MulenPay-Signature': 'should-not-help-either',
            'Authorization': f'Bearer {SECRET}',
            'X-MulenPay-Token': SECRET,
        },
    )

    assert _verify_mulenpay_signature(request, body) is False


def test_verify_is_case_insensitive_for_hex_sign() -> None:
    data = {'id': 7, 'amount': '50.00', 'currency': 'rub', 'uuid': 'x', 'payment_status': 'success'}
    expected = _sign(data)
    payload = {**data, 'sign': expected.upper()}
    body = json.dumps(payload).encode('utf-8')

    assert _verify_mulenpay_signature(_build_request(body), body) is True


def test_verify_handles_unicode_values_in_payload() -> None:
    data = {
        'id': 1,
        'amount': '1.00',
        'description': 'Пополнение СБП',
        'uuid': 'mulen_1',
        'payment_status': 'success',
    }
    payload = {**data, 'sign': _sign(data)}
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')

    assert _verify_mulenpay_signature(_build_request(body), body) is True


def test_verify_handles_extra_unknown_fields() -> None:
    """If MulenPay adds new fields, formula should still work — SDK iterates all values."""
    data = {
        'id': 1,
        'amount': '1.00',
        'currency': 'rub',
        'uuid': 'u',
        'payment_status': 'success',
        'unknown_future_field': 'whatever',
    }
    payload = {**data, 'sign': _sign(data)}
    body = json.dumps(payload).encode('utf-8')

    assert _verify_mulenpay_signature(_build_request(body), body) is True


class DummyBot:
    pass


def _get_route(router, path: str, method: str = 'POST'):
    for route in router.routes:
        if getattr(route, 'path', '') == path and method in getattr(route, 'methods', set()):
            return route
    raise AssertionError(f'Route {path} with method {method} not found')


@pytest.mark.anyio
async def test_route_returns_200_on_valid_sign(monkeypatch: pytest.MonkeyPatch) -> None:
    data = {'id': 1, 'amount': '100.00', 'currency': 'rub', 'uuid': 'u', 'payment_status': 'success'}
    payload = {**data, 'sign': _sign(data)}
    body = json.dumps(payload).encode('utf-8')

    payment_service = SimpleNamespace(process_mulenpay_callback=AsyncMock(return_value=True))

    async def fake_callback(svc, payload_arg, method):
        return await svc.process_mulenpay_callback(None, payload_arg)

    monkeypatch.setattr('app.webserver.payments._process_payment_service_callback', fake_callback)

    router = create_payment_router(DummyBot(), payment_service)
    assert router is not None
    route = _get_route(router, '/mulen')

    request = _build_request(body)
    response = await route.endpoint(request)

    assert response.status_code == 200
    assert json.loads(response.body.decode('utf-8'))['status'] == 'ok'
    payment_service.process_mulenpay_callback.assert_awaited_once()


@pytest.mark.anyio
async def test_route_returns_401_on_invalid_sign(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({'id': 1, 'amount': '100.00', 'sign': 'bad'}).encode('utf-8')

    payment_service = SimpleNamespace(process_mulenpay_callback=AsyncMock())

    router = create_payment_router(DummyBot(), payment_service)
    assert router is not None
    route = _get_route(router, '/mulen')

    response = await route.endpoint(_build_request(body))

    assert response.status_code == 401
    assert json.loads(response.body.decode('utf-8'))['reason'] == 'invalid_signature'
    payment_service.process_mulenpay_callback.assert_not_awaited()


@pytest.mark.anyio
async def test_route_returns_401_when_sign_missing_from_body() -> None:
    body = json.dumps({'id': 1, 'amount': '100.00'}).encode('utf-8')

    payment_service = SimpleNamespace(process_mulenpay_callback=AsyncMock())

    router = create_payment_router(DummyBot(), payment_service)
    assert router is not None
    route = _get_route(router, '/mulen')

    response = await route.endpoint(_build_request(body))

    assert response.status_code == 401
    payment_service.process_mulenpay_callback.assert_not_awaited()
