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

In September 2026 a second provider callback shape was observed without
``sign``. Those callbacks are never trusted directly: they may only trigger an
authenticated MulenPay API re-check of an already known payment before the
normal payment processor runs.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.config import settings
from app.webserver import payments
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
    monkeypatch.setattr(payments, '_webhook_callback_semaphore', None)


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


def _status_mapper(status_code: int) -> str:
    return {
        0: 'created',
        1: 'processing',
        2: 'canceled',
        3: 'success',
        4: 'error',
        5: 'hold',
        6: 'hold',
    }.get(status_code, 'unknown')


@pytest.mark.anyio
async def test_route_returns_200_on_valid_sign(monkeypatch: pytest.MonkeyPatch) -> None:
    data = {'id': 1, 'amount': '100.00', 'currency': 'rub', 'uuid': 'u', 'payment_status': 'success'}
    payload = {**data, 'sign': _sign(data)}
    body = json.dumps(payload).encode('utf-8')

    signed_service = SimpleNamespace(process_mulenpay_callback=AsyncMock(return_value=True))

    async def fake_callback(svc, payload_arg, method):
        return await svc.process_mulenpay_callback(None, payload_arg)

    monkeypatch.setattr(payments, '_process_payment_service_callback', fake_callback)

    router = create_payment_router(DummyBot(), signed_service)
    assert router is not None
    route = _get_route(router, '/mulen')

    response = await route.endpoint(_build_request(body))

    assert response.status_code == 200
    assert json.loads(response.body.decode('utf-8'))['status'] == 'ok'
    signed_service.process_mulenpay_callback.assert_awaited_once()

    db = SimpleNamespace()
    local_payment = SimpleNamespace(
        mulen_payment_id=123,
        amount_kopeks=10_000,
        currency='RUB',
        uuid='merchant-side-uuid',
    )
    provider = SimpleNamespace(
        get_payment=AsyncMock(
            return_value={
                'success': True,
                'payment': {
                    'id': 123,
                    'amount': '100.00',
                    'currency': 'rub',
                    'status': 3,
                    'uuid': 'provider-side-uuid',
                },
            }
        )
    )
    unsigned_service = SimpleNamespace(
        mulenpay_service=provider,
        _map_mulenpay_status=_status_mapper,
        process_mulenpay_callback=AsyncMock(return_value=True),
    )

    async def fake_get_db():
        yield db

    async def fake_lookup(_db, provider_id: int):
        assert _db is db
        assert provider_id == 123
        return local_payment

    monkeypatch.setattr(payments, 'get_db', fake_get_db)
    monkeypatch.setattr(payments, 'get_mulenpay_payment_by_mulen_id', fake_lookup)

    unsigned_router = create_payment_router(DummyBot(), unsigned_service)
    assert unsigned_router is not None
    unsigned_route = _get_route(unsigned_router, '/mulen')
    unsigned_payload = {
        'id': 123,
        'amount': '100.00',
        'currency': 'rub',
        'uuid': 'merchant-side-uuid',
        'payment_status': 'success',
    }

    unsigned_response = await unsigned_route.endpoint(_build_request(json.dumps(unsigned_payload).encode('utf-8')))

    assert unsigned_response.status_code == 200
    assert json.loads(unsigned_response.body.decode('utf-8'))['status'] == 'ok'
    provider.get_payment.assert_awaited_once_with(123)
    unsigned_service.process_mulenpay_callback.assert_awaited_once_with(
        db,
        {
            'id': 123,
            'amount': '100.00',
            'currency': 'rub',
            'payment_status': 'success',
        },
    )

    provider.get_payment.reset_mock()
    provider.get_payment.return_value = None
    unsigned_service.process_mulenpay_callback.reset_mock()

    unavailable_response = await unsigned_route.endpoint(_build_request(json.dumps(unsigned_payload).encode('utf-8')))

    assert unavailable_response.status_code == 503
    assert json.loads(unavailable_response.body.decode('utf-8'))['reason'] == 'verification_unavailable'
    provider.get_payment.assert_awaited_once_with(123)
    unsigned_service.process_mulenpay_callback.assert_not_awaited()


@pytest.mark.anyio
async def test_route_returns_401_on_invalid_sign(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps(
        {
            'id': 1,
            'amount': '100.00',
            'currency': 'rub',
            'payment_status': 'success',
            'sign': 'bad',
        }
    ).encode('utf-8')

    payment_service = SimpleNamespace(process_mulenpay_callback=AsyncMock())
    unsigned_fallback = AsyncMock()
    monkeypatch.setattr(payments, '_process_unsigned_mulenpay_callback', unsigned_fallback)

    router = create_payment_router(DummyBot(), payment_service)
    assert router is not None
    route = _get_route(router, '/mulen')

    response = await route.endpoint(_build_request(body))

    assert response.status_code == 401
    assert json.loads(response.body.decode('utf-8'))['reason'] == 'invalid_signature'
    unsigned_fallback.assert_not_awaited()
    payment_service.process_mulenpay_callback.assert_not_awaited()


@pytest.mark.anyio
async def test_route_returns_401_when_sign_missing_from_body(monkeypatch: pytest.MonkeyPatch) -> None:
    incomplete_body = json.dumps({'id': 1, 'amount': '100.00'}).encode('utf-8')

    payment_service = SimpleNamespace(process_mulenpay_callback=AsyncMock())

    router = create_payment_router(DummyBot(), payment_service)
    assert router is not None
    route = _get_route(router, '/mulen')

    incomplete_response = await route.endpoint(_build_request(incomplete_body))

    assert incomplete_response.status_code == 401
    payment_service.process_mulenpay_callback.assert_not_awaited()

    array_response = await route.endpoint(_build_request(json.dumps([1, 2, 3]).encode('utf-8')))

    assert array_response.status_code == 401
    assert json.loads(array_response.body.decode('utf-8'))['reason'] == 'invalid_signature'

    db = SimpleNamespace()
    local_payment = SimpleNamespace(mulen_payment_id=123, amount_kopeks=10_000, currency='RUB')
    provider = SimpleNamespace(
        get_payment=AsyncMock(
            return_value={
                'success': True,
                'payment': {
                    'id': 123,
                    'amount': '101.00',
                    'currency': 'rub',
                    'status': 3,
                },
            }
        )
    )
    verified_service = SimpleNamespace(
        mulenpay_service=provider,
        _map_mulenpay_status=_status_mapper,
        process_mulenpay_callback=AsyncMock(),
    )

    async def fake_get_db():
        yield db

    async def fake_lookup(_db, _provider_id: int):
        return local_payment

    monkeypatch.setattr(payments, 'get_db', fake_get_db)
    monkeypatch.setattr(payments, 'get_mulenpay_payment_by_mulen_id', fake_lookup)

    verified_router = create_payment_router(DummyBot(), verified_service)
    assert verified_router is not None
    verified_route = _get_route(verified_router, '/mulen')
    unsigned_payload = {'id': 123, 'amount': '100.00', 'currency': 'rub', 'payment_status': 'success'}

    mismatch_response = await verified_route.endpoint(_build_request(json.dumps(unsigned_payload).encode('utf-8')))

    assert mismatch_response.status_code == 401
    assert json.loads(mismatch_response.body.decode('utf-8'))['reason'] == 'verification_failed'
    verified_service.process_mulenpay_callback.assert_not_awaited()
