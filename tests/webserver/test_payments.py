import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.config import settings
from app.webserver.payments import create_payment_router


class DummyBot:
    pass


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'TRIBUTE_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'TRIBUTE_API_KEY', None, raising=False)
    monkeypatch.setattr(settings, 'TRIBUTE_WEBHOOK_PATH', '/tribute', raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_WEBHOOK_PATH', '/mulen', raising=False)
    monkeypatch.setattr(settings, 'CRYPTOBOT_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'CRYPTOBOT_API_TOKEN', None, raising=False)
    monkeypatch.setattr(settings, 'CRYPTOBOT_WEBHOOK_PATH', '/cryptobot', raising=False)
    monkeypatch.setattr(settings, 'CRYPTOBOT_WEBHOOK_SECRET', None, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_WEBHOOK_PATH', '/yookassa', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_SHOP_ID', 'shop', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_SECRET_KEY', 'key', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_TRUSTED_PROXY_NETWORKS', '', raising=False)
    monkeypatch.setattr(settings, 'SHKEEPER_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'SHKEEPER_API_KEY', None, raising=False)
    monkeypatch.setattr(settings, 'SHKEEPER_CALLBACK_API_KEY', None, raising=False)
    monkeypatch.setattr(settings, 'SHKEEPER_WEBHOOK_PATH', '/shkeeper-webhook', raising=False)
    monkeypatch.setattr(settings, 'WEBHOOK_URL', 'http://test', raising=False)


def _get_route(router, path: str, method: str = 'POST'):
    for route in router.routes:
        if getattr(route, 'path', '') == path and method in getattr(route, 'methods', set()):
            return route
    raise AssertionError(f'Route {path} with method {method} not found')


def _build_request(
    path: str,
    body: bytes,
    headers: dict[str, str],
    client_ip: str | None = '185.71.76.1',
) -> Request:
    scope = {
        'type': 'http',
        'asgi': {'version': '3.0'},
        'method': 'POST',
        'path': path,
        'headers': [(k.lower().encode('latin-1'), v.encode('latin-1')) for k, v in headers.items()],
    }

    if client_ip is not None:
        scope['client'] = (client_ip, 12345)

    async def receive() -> dict:
        return {'type': 'http.request', 'body': body, 'more_body': False}

    return Request(scope, receive)


@pytest.mark.anyio
async def test_tribute_webhook_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'TRIBUTE_ENABLED', True, raising=False)

    process_mock = AsyncMock(return_value={'status': 'ok'})

    class StubTributeService:
        def __init__(self, *_args, **_kwargs):
            pass

        async def process_webhook(self, payload: str):  # type: ignore[override]
            return await process_mock(payload)

    class StubTributeAPI:
        @staticmethod
        def verify_webhook_signature(payload: str, signature: str) -> bool:
            return True

    monkeypatch.setattr('app.webserver.payments.TributeService', StubTributeService)
    monkeypatch.setattr('app.webserver.payments.TributeAPI', StubTributeAPI)

    router = create_payment_router(DummyBot(), SimpleNamespace())
    assert router is not None

    route = _get_route(router, settings.TRIBUTE_WEBHOOK_PATH)
    request = _build_request(
        settings.TRIBUTE_WEBHOOK_PATH,
        body=json.dumps({'event': 'payment'}).encode('utf-8'),
        headers={'trbt-signature': 'sig'},
    )

    response = await route.endpoint(request)

    assert response.status_code == 200
    assert json.loads(response.body.decode('utf-8'))['status'] == 'ok'
    process_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_yookassa_unknown_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_ENABLED', True, raising=False)

    service = SimpleNamespace(process_yookassa_webhook=AsyncMock())

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=json.dumps({'event': 'payment.succeeded'}).encode('utf-8'),
        headers={},
        client_ip=None,
    )

    response = await route.endpoint(request)

    assert response.status_code == 403
    payload = json.loads(response.body.decode('utf-8'))
    assert payload['reason'] == 'unknown_ip'
    service.process_yookassa_webhook.assert_not_awaited()


@pytest.mark.anyio
async def test_yookassa_forbidden_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_ENABLED', True, raising=False)

    service = SimpleNamespace(process_yookassa_webhook=AsyncMock())

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=json.dumps({'event': 'payment.succeeded'}).encode('utf-8'),
        headers={},
        client_ip='8.8.8.8',
    )

    response = await route.endpoint(request)

    assert response.status_code == 403
    payload = json.loads(response.body.decode('utf-8'))
    assert payload['reason'] == 'forbidden_ip'
    assert payload['ip'] == '8.8.8.8'
    service.process_yookassa_webhook.assert_not_awaited()


@pytest.mark.anyio
async def test_yookassa_forbidden_ip_ignores_spoofed_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_ENABLED', True, raising=False)

    service = SimpleNamespace(process_yookassa_webhook=AsyncMock())

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=json.dumps({'event': 'payment.succeeded'}).encode('utf-8'),
        headers={'X-Forwarded-For': '185.71.76.10'},
        client_ip='8.8.8.8',
    )

    response = await route.endpoint(request)

    assert response.status_code == 403
    payload = json.loads(response.body.decode('utf-8'))
    assert payload['reason'] == 'forbidden_ip'
    assert payload['ip'] == '8.8.8.8'
    service.process_yookassa_webhook.assert_not_awaited()


@pytest.mark.anyio
async def test_yookassa_forbidden_ip_ignores_spoofed_forwarded_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_ENABLED', True, raising=False)

    service = SimpleNamespace(process_yookassa_webhook=AsyncMock())

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=json.dumps({'event': 'payment.succeeded'}).encode('utf-8'),
        headers={'X-Forwarded-For': '185.71.76.10, 8.8.8.8'},
        client_ip='10.0.0.5',
    )

    response = await route.endpoint(request)

    assert response.status_code == 403
    payload = json.loads(response.body.decode('utf-8'))
    assert payload['reason'] == 'forbidden_ip'
    assert payload['ip'] == '8.8.8.8'
    service.process_yookassa_webhook.assert_not_awaited()


@pytest.mark.anyio
async def test_yookassa_allowed_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_ENABLED', True, raising=False)

    async def fake_get_db():
        yield SimpleNamespace()

    monkeypatch.setattr('app.webserver.payments.get_db', fake_get_db)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=json.dumps({'event': 'payment.succeeded'}).encode('utf-8'),
        headers={},
        client_ip='185.71.76.10',
    )

    response = await route.endpoint(request)

    assert response.status_code == 200
    payload = json.loads(response.body.decode('utf-8'))
    assert payload['status'] == 'ok'
    process_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_yookassa_allowed_via_forwarded_header_when_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_ENABLED', True, raising=False)

    async def fake_get_db():
        yield SimpleNamespace()

    monkeypatch.setattr('app.webserver.payments.get_db', fake_get_db)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=json.dumps({'event': 'payment.succeeded'}).encode('utf-8'),
        headers={'X-Forwarded-For': '185.71.76.10'},
        client_ip='10.0.0.5',
    )

    response = await route.endpoint(request)

    assert response.status_code == 200
    payload = json.loads(response.body.decode('utf-8'))
    assert payload['status'] == 'ok'
    process_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_yookassa_allowed_via_cf_connecting_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_ENABLED', True, raising=False)

    async def fake_get_db():
        yield SimpleNamespace()

    monkeypatch.setattr('app.webserver.payments.get_db', fake_get_db)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=json.dumps({'event': 'payment.succeeded'}).encode('utf-8'),
        headers={'Cf-Connecting-Ip': '185.71.76.10'},
        client_ip='172.64.223.133',
    )

    response = await route.endpoint(request)

    assert response.status_code == 200
    payload = json.loads(response.body.decode('utf-8'))
    assert payload['status'] == 'ok'
    process_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_yookassa_allowed_via_trusted_forwarded_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_TRUSTED_PROXY_NETWORKS', '203.0.113.0/24', raising=False)

    async def fake_get_db():
        yield SimpleNamespace()

    monkeypatch.setattr('app.webserver.payments.get_db', fake_get_db)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=json.dumps({'event': 'payment.succeeded'}).encode('utf-8'),
        headers={'X-Forwarded-For': '185.71.76.10, 203.0.113.10'},
        client_ip='10.0.0.5',
    )

    response = await route.endpoint(request)

    assert response.status_code == 200
    payload = json.loads(response.body.decode('utf-8'))
    assert payload['status'] == 'ok'
    process_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_yookassa_allowed_via_trusted_public_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_TRUSTED_PROXY_NETWORKS', '198.51.100.0/24', raising=False)

    async def fake_get_db():
        yield SimpleNamespace()

    monkeypatch.setattr('app.webserver.payments.get_db', fake_get_db)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=json.dumps({'event': 'payment.succeeded'}).encode('utf-8'),
        headers={'X-Forwarded-For': '185.71.76.10, 198.51.100.10'},
        client_ip='198.51.100.20',
    )

    response = await route.endpoint(request)

    assert response.status_code == 200
    payload = json.loads(response.body.decode('utf-8'))
    assert payload['status'] == 'ok'
    process_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_yookassa_webhook_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_ENABLED', True, raising=False)

    async def fake_get_db():
        yield SimpleNamespace()

    monkeypatch.setattr('app.webserver.payments.get_db', fake_get_db)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    payload = {'event': 'payment.succeeded'}
    body = json.dumps(payload).encode('utf-8')
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=body,
        headers={},
    )

    response = await route.endpoint(request)

    assert response.status_code == 200
    payload = json.loads(response.body.decode('utf-8'))
    assert payload['status'] == 'ok'
    process_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_yookassa_webhook_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_ENABLED', True, raising=False)

    async def fake_get_db():
        yield SimpleNamespace()

    monkeypatch.setattr('app.webserver.payments.get_db', fake_get_db)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    payload = {'event': 'payment.canceled'}
    body = json.dumps(payload).encode('utf-8')
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=body,
        headers={},
    )

    response = await route.endpoint(request)

    assert response.status_code == 200
    payload = json.loads(response.body.decode('utf-8'))
    assert payload['status'] == 'ok'
    process_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_yookassa_webhook_with_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_ENABLED', True, raising=False)

    async def fake_get_db():
        yield SimpleNamespace()

    monkeypatch.setattr('app.webserver.payments.get_db', fake_get_db)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.YOOKASSA_WEBHOOK_PATH)
    payload = {'event': 'payment.succeeded'}
    body = json.dumps(payload).encode('utf-8')
    request = _build_request(
        settings.YOOKASSA_WEBHOOK_PATH,
        body=body,
        headers={'Signature': 'dummy'},
    )

    response = await route.endpoint(request)

    assert response.status_code == 200
    payload = json.loads(response.body.decode('utf-8'))
    assert payload['status'] == 'ok'
    process_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_cryptobot_missing_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CRYPTOBOT_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'CRYPTOBOT_API_TOKEN', 'token', raising=False)
    monkeypatch.setattr(settings, 'CRYPTOBOT_WEBHOOK_SECRET', 'secret', raising=False)

    router = create_payment_router(DummyBot(), SimpleNamespace())
    assert router is not None

    route = _get_route(router, settings.CRYPTOBOT_WEBHOOK_PATH)
    request = _build_request(
        settings.CRYPTOBOT_WEBHOOK_PATH,
        body=json.dumps({'test': 'value'}).encode('utf-8'),
        headers={},
    )

    response = await route.endpoint(request)

    assert response.status_code == 401
    payload = json.loads(response.body.decode('utf-8'))
    assert payload['reason'] == 'missing_signature'


@pytest.mark.anyio
async def test_cryptobot_invalid_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CRYPTOBOT_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'CRYPTOBOT_API_TOKEN', 'token', raising=False)
    monkeypatch.setattr(settings, 'CRYPTOBOT_WEBHOOK_SECRET', 'secret', raising=False)

    class StubCryptoBotService:
        @staticmethod
        def verify_webhook_signature(body: str, signature: str) -> bool:
            return False

    monkeypatch.setattr('app.external.cryptobot.CryptoBotService', StubCryptoBotService)

    router = create_payment_router(DummyBot(), SimpleNamespace())
    assert router is not None

    route = _get_route(router, settings.CRYPTOBOT_WEBHOOK_PATH)
    request = _build_request(
        settings.CRYPTOBOT_WEBHOOK_PATH,
        body=json.dumps({'test': 'value'}).encode('utf-8'),
        headers={'Crypto-Pay-API-Signature': 'sig'},
    )

    response = await route.endpoint(request)

    assert response.status_code == 401
    payload = json.loads(response.body.decode('utf-8'))
    assert payload['reason'] == 'invalid_signature'


@pytest.mark.anyio
async def test_shkeeper_webhook_invalid_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'SHKEEPER_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'SHKEEPER_API_KEY', 'api-key', raising=False)
    monkeypatch.setattr(settings, 'SHKEEPER_CALLBACK_API_KEY', 'callback-key', raising=False)

    service = SimpleNamespace(
        shkeeper_service=SimpleNamespace(verify_callback_auth=lambda _header: False),
        process_shkeeper_webhook=AsyncMock(return_value=True),
    )

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.SHKEEPER_WEBHOOK_PATH)
    request = _build_request(
        settings.SHKEEPER_WEBHOOK_PATH,
        body=json.dumps({'id': 'inv'}).encode('utf-8'),
        headers={'X-Shkeeper-API-Key': 'wrong'},
    )

    response = await route.endpoint(request)

    assert response.status_code == 401
    payload = json.loads(response.body.decode('utf-8'))
    assert payload['reason'] == 'invalid_signature'
    service.process_shkeeper_webhook.assert_not_awaited()


@pytest.mark.anyio
async def test_shkeeper_webhook_processing_exception_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'SHKEEPER_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'SHKEEPER_API_KEY', 'api-key', raising=False)
    monkeypatch.setattr(settings, 'SHKEEPER_CALLBACK_API_KEY', 'callback-key', raising=False)

    async def fake_get_db():
        yield SimpleNamespace()

    monkeypatch.setattr('app.webserver.payments.get_db', fake_get_db)

    service = SimpleNamespace(
        shkeeper_service=SimpleNamespace(verify_callback_auth=lambda _header: True),
        process_shkeeper_webhook=AsyncMock(side_effect=RuntimeError('boom')),
    )

    router = create_payment_router(DummyBot(), service)
    assert router is not None

    route = _get_route(router, settings.SHKEEPER_WEBHOOK_PATH)
    request = _build_request(
        settings.SHKEEPER_WEBHOOK_PATH,
        body=json.dumps({'id': 'inv'}).encode('utf-8'),
        headers={'X-Shkeeper-API-Key': 'callback-key'},
    )

    response = await route.endpoint(request)

    assert response.status_code == 400
    payload = json.loads(response.body.decode('utf-8'))
    assert payload['reason'] == 'not_processed'
