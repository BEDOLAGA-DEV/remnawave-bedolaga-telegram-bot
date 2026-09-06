"""Regression tests for MulenPay callbacks that arrive without ``sign``.

The webhook body is not trusted in this mode.  It is only a trigger to look up
an already known provider payment and re-read its status through the
API-key-authenticated MulenPay API before the normal payment processor runs.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.config import settings
from app.webserver import payments


SECRET = 'test-secret-key'


@pytest.fixture(autouse=True)
def mulenpay_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'MULENPAY_SECRET_KEY', SECRET, raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_API_KEY', 'k', raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_SHOP_ID', 1, raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_WEBHOOK_PATH', '/mulen', raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_DISPLAY_NAME', 'MulenPay', raising=False)
    monkeypatch.setattr(payments, '_webhook_callback_semaphore', None)


def _build_request(body: bytes) -> Request:
    scope = {
        'type': 'http',
        'asgi': {'version': '3.0'},
        'method': 'POST',
        'path': '/mulen',
        'headers': [],
        'client': ('127.0.0.1', 12345),
    }

    async def receive() -> dict:
        return {'type': 'http.request', 'body': body, 'more_body': False}

    return Request(scope, receive)


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


def test_amount_parser_requires_exact_kopeks() -> None:
    assert payments._mulenpay_amount_to_kopeks('100.00') == 10_000
    assert payments._mulenpay_amount_to_kopeks(12.34) == 1_234
    assert payments._mulenpay_amount_to_kopeks('1.001') is None
    assert payments._mulenpay_amount_to_kopeks('NaN') is None
    assert payments._mulenpay_amount_to_kopeks('not-a-number') is None


def test_verified_payload_uses_provider_id_amount_currency_but_not_uuid() -> None:
    local = SimpleNamespace(
        mulen_payment_id=123,
        amount_kopeks=10_000,
        currency='RUB',
        uuid='merchant-generated-uuid',
    )
    service = SimpleNamespace(_map_mulenpay_status=_status_mapper)
    remote = {
        'id': 123,
        'amount': '100.00',
        'currency': 'rub',
        'status': 3,
        # MulenPay may return a UUID that differs from the merchant UUID.
        'uuid': 'provider-side-uuid',
    }

    result = payments._build_verified_mulenpay_callback_payload(service, local, remote)

    assert result == {
        'id': 123,
        'amount': '100.00',
        'currency': 'rub',
        'payment_status': 'success',
    }


@pytest.mark.parametrize(
    'remote',
    [
        {'id': 999, 'amount': '100.00', 'currency': 'rub', 'status': 3},
        {'id': 123, 'amount': '101.00', 'currency': 'rub', 'status': 3},
        {'id': 123, 'amount': '100.00', 'currency': 'usd', 'status': 3},
        {'id': 123, 'amount': '100.00', 'currency': 'rub', 'status': 99},
    ],
)
def test_verified_payload_rejects_provider_mismatch(remote: dict) -> None:
    local = SimpleNamespace(mulen_payment_id=123, amount_kopeks=10_000, currency='RUB')
    service = SimpleNamespace(_map_mulenpay_status=_status_mapper)

    assert payments._build_verified_mulenpay_callback_payload(service, local, remote) is None


@pytest.mark.anyio
async def test_unsigned_callback_rechecks_provider_before_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    db = SimpleNamespace()
    local = SimpleNamespace(mulen_payment_id=123, amount_kopeks=10_000, currency='RUB')
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
    service = SimpleNamespace(
        mulenpay_service=provider,
        _map_mulenpay_status=_status_mapper,
        process_mulenpay_callback=AsyncMock(return_value=True),
    )

    async def fake_get_db():
        yield db

    async def fake_lookup(_db, provider_id: int):
        assert _db is db
        assert provider_id == 123
        return local

    monkeypatch.setattr(payments, 'get_db', fake_get_db)
    monkeypatch.setattr(payments, 'get_mulenpay_payment_by_mulen_id', fake_lookup)

    result = await payments._process_unsigned_mulenpay_callback(
        service,
        {'id': 123, 'amount': '100.00', 'currency': 'rub', 'payment_status': 'success'},
    )

    assert result == payments._MULENPAY_UNSIGNED_PROCESSED
    provider.get_payment.assert_awaited_once_with(123)
    service.process_mulenpay_callback.assert_awaited_once_with(
        db,
        {
            'id': 123,
            'amount': '100.00',
            'currency': 'rub',
            'payment_status': 'success',
        },
    )


@pytest.mark.anyio
async def test_unsigned_callback_rejects_unknown_local_payment(monkeypatch: pytest.MonkeyPatch) -> None:
    db = SimpleNamespace()
    provider = SimpleNamespace(get_payment=AsyncMock())
    service = SimpleNamespace(
        mulenpay_service=provider,
        _map_mulenpay_status=_status_mapper,
        process_mulenpay_callback=AsyncMock(),
    )

    async def fake_get_db():
        yield db

    async def fake_lookup(_db, _provider_id: int):
        return None

    monkeypatch.setattr(payments, 'get_db', fake_get_db)
    monkeypatch.setattr(payments, 'get_mulenpay_payment_by_mulen_id', fake_lookup)

    result = await payments._process_unsigned_mulenpay_callback(
        service,
        {'id': 123, 'amount': '100.00', 'currency': 'rub', 'payment_status': 'success'},
    )

    assert result == payments._MULENPAY_UNSIGNED_REJECTED
    provider.get_payment.assert_not_awaited()
    service.process_mulenpay_callback.assert_not_awaited()


@pytest.mark.anyio
async def test_unsigned_callback_retries_when_provider_api_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    db = SimpleNamespace()
    local = SimpleNamespace(mulen_payment_id=123, amount_kopeks=10_000, currency='RUB')
    provider = SimpleNamespace(get_payment=AsyncMock(return_value=None))
    service = SimpleNamespace(
        mulenpay_service=provider,
        _map_mulenpay_status=_status_mapper,
        process_mulenpay_callback=AsyncMock(),
    )

    async def fake_get_db():
        yield db

    async def fake_lookup(_db, _provider_id: int):
        return local

    monkeypatch.setattr(payments, 'get_db', fake_get_db)
    monkeypatch.setattr(payments, 'get_mulenpay_payment_by_mulen_id', fake_lookup)

    result = await payments._process_unsigned_mulenpay_callback(
        service,
        {'id': 123, 'amount': '100.00', 'currency': 'rub', 'payment_status': 'success'},
    )

    assert result == payments._MULENPAY_UNSIGNED_RETRY
    service.process_mulenpay_callback.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ('outcome', 'expected_status', 'expected_reason'),
    [
        ('processed', 200, None),
        ('retry', 503, 'verification_unavailable'),
        ('rejected', 401, 'verification_failed'),
        ('failed', 500, 'processing_failed'),
    ],
)
async def test_route_handles_unsigned_callback_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    expected_status: int,
    expected_reason: str | None,
) -> None:
    payload = {'id': 123, 'amount': '100.00', 'currency': 'rub', 'payment_status': 'success'}
    unsigned = AsyncMock(return_value=outcome)
    monkeypatch.setattr(payments, '_process_unsigned_mulenpay_callback', unsigned)

    service = SimpleNamespace(process_mulenpay_callback=AsyncMock())
    router = payments.create_payment_router(SimpleNamespace(), service)
    assert router is not None
    route = _get_route(router, '/mulen')

    response = await route.endpoint(_build_request(json.dumps(payload).encode()))

    assert response.status_code == expected_status
    body = json.loads(response.body.decode())
    if expected_reason is None:
        assert body['status'] == 'ok'
    else:
        assert body['reason'] == expected_reason
    unsigned.assert_awaited_once_with(service, payload)
    service.process_mulenpay_callback.assert_not_awaited()


@pytest.mark.anyio
async def test_route_does_not_fallback_when_invalid_sign_is_present(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        'id': 123,
        'amount': '100.00',
        'currency': 'rub',
        'payment_status': 'success',
        'sign': 'bad',
    }
    unsigned = AsyncMock()
    monkeypatch.setattr(payments, '_process_unsigned_mulenpay_callback', unsigned)

    service = SimpleNamespace(process_mulenpay_callback=AsyncMock())
    router = payments.create_payment_router(SimpleNamespace(), service)
    assert router is not None
    route = _get_route(router, '/mulen')

    response = await route.endpoint(_build_request(json.dumps(payload).encode()))

    assert response.status_code == 401
    assert json.loads(response.body.decode())['reason'] == 'invalid_signature'
    unsigned.assert_not_awaited()
    service.process_mulenpay_callback.assert_not_awaited()


@pytest.mark.anyio
async def test_route_keeps_rejecting_incomplete_unsigned_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {'id': 123, 'amount': '100.00'}
    unsigned = AsyncMock()
    monkeypatch.setattr(payments, '_process_unsigned_mulenpay_callback', unsigned)

    service = SimpleNamespace(process_mulenpay_callback=AsyncMock())
    router = payments.create_payment_router(SimpleNamespace(), service)
    assert router is not None
    route = _get_route(router, '/mulen')

    response = await route.endpoint(_build_request(json.dumps(payload).encode()))

    assert response.status_code == 401
    unsigned.assert_not_awaited()
