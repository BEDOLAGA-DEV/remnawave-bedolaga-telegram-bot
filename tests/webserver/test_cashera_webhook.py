"""Эндпоинт вебхука Cashera: подлинность по X-Api-Key + X-Secret и коды ответа.

Cashera не повторяет 4xx и повторяет 5xx (до 3 раз). Поэтому: чужие заголовки —
401 без обработки; обработано — 200; сбой, который повтор может вылечить, — 500.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.config import settings
from app.webserver import payments as payments_module
from app.webserver.payments import create_payment_router


class DummyBot:
    pass


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        'TRIBUTE_ENABLED': False,
        'TRIBUTE_API_KEY': None,
        'CRYPTOBOT_ENABLED': False,
        'CRYPTOBOT_API_TOKEN': None,
        'YOOKASSA_ENABLED': False,
        'YOOKASSA_SHOP_ID': 'shop',
        'YOOKASSA_SECRET_KEY': 'key',
        'WEBHOOK_URL': 'https://bot.example.com',
        'CASHERA_ENABLED': True,
        'CASHERA_API_KEY': 'pk_test',
        'CASHERA_API_SECRET': 'sk_test',
        'CASHERA_WEBHOOK_PATH': '/cashera-webhook',
    }.items():
        monkeypatch.setattr(settings, key, value, raising=False)


def _route(router):
    for route in router.routes:
        if getattr(route, 'path', '') == '/cashera-webhook' and 'POST' in getattr(route, 'methods', set()):
            return route
    raise AssertionError('Cashera webhook route not registered')


def _request(body: bytes, headers: dict[str, str]) -> Request:
    scope = {
        'type': 'http',
        'asgi': {'version': '3.0'},
        'method': 'POST',
        'path': '/cashera-webhook',
        'headers': [(k.lower().encode('latin-1'), v.encode('latin-1')) for k, v in headers.items()],
        'client': ('203.0.113.7', 443),
    }

    async def receive() -> dict:
        return {'type': 'http.request', 'body': body, 'more_body': False}

    return Request(scope, receive)


BODY = json.dumps(
    {
        'event': 'transaction.status_updated',
        'transaction': {'uuid': 'u-1', 'external_id': 'cas555_x', 'status': 'paid', 'amount': 49900, 'currency': 'RUB'},
    }
).encode()


def _recorder(monkeypatch, result):
    calls: list[str] = []

    async def fake(_ps, _payload, method_name):
        calls.append(method_name)
        return result

    monkeypatch.setattr(payments_module, '_process_payment_service_callback', fake)
    return calls


@pytest.mark.anyio
@pytest.mark.parametrize(
    'headers',
    [
        {'X-Api-Key': 'pk_test', 'X-Secret': 'sk_wrong'},
        {'X-Api-Key': 'pk_wrong', 'X-Secret': 'sk_test'},
        {},
    ],
)
async def test_foreign_credentials_get_401_and_are_not_processed(monkeypatch, headers):
    calls = _recorder(monkeypatch, True)
    route = _route(create_payment_router(DummyBot(), SimpleNamespace()))

    response = await route.endpoint(_request(BODY, headers))

    assert response.status_code == 401
    assert calls == []


@pytest.mark.anyio
async def test_processed_event_gets_200(monkeypatch):
    calls = _recorder(monkeypatch, True)
    route = _route(create_payment_router(DummyBot(), SimpleNamespace()))

    response = await route.endpoint(_request(BODY, {'X-Api-Key': 'pk_test', 'X-Secret': 'sk_test'}))

    assert response.status_code == 200
    assert calls == ['process_cashera_webhook']


@pytest.mark.anyio
async def test_retryable_failure_gets_500(monkeypatch):
    _recorder(monkeypatch, False)
    route = _route(create_payment_router(DummyBot(), SimpleNamespace()))

    response = await route.endpoint(_request(BODY, {'X-Api-Key': 'pk_test', 'X-Secret': 'sk_test'}))

    assert response.status_code == 500


@pytest.mark.anyio
async def test_route_is_not_registered_without_credentials(monkeypatch):
    monkeypatch.setattr(settings, 'CASHERA_API_SECRET', None, raising=False)
    router = create_payment_router(DummyBot(), SimpleNamespace())
    paths = {getattr(route, 'path', '') for route in (router.routes if router else [])}
    assert '/cashera-webhook' not in paths
