import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.config import settings
from app.external.yookassa_webhook import (
    create_yookassa_webhook_app,
    resolve_webhook_client_ip,
)


ALLOWED_IP = '185.71.76.10'


class DummyDB:
    async def close(self) -> None:  # pragma: no cover - simple stub
        pass


@pytest.fixture(autouse=True)
def configure_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_SHOP_ID', 'shop', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_SECRET_KEY', 'key', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_WEBHOOK_PATH', '/yookassa-webhook', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_TRUSTED_PROXY_NETWORKS', '', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_SKIP_IP_CHECK', False, raising=False)


def _build_headers(**overrides: str) -> dict[str, str]:
    headers = {
        'Content-Type': 'application/json',
        'X-Forwarded-For': ALLOWED_IP,
        'Cf-Connecting-Ip': ALLOWED_IP,
    }
    headers.update(overrides)
    return headers


@pytest.mark.parametrize(
    ('remote', 'expected'),
    (
        ('185.71.76.10', '185.71.76.10'),  # публичный peer — отправитель он сам
        ('8.8.8.8', '8.8.8.8'),  # заголовки от публичного peer не читаются
        ('10.0.0.5', '185.71.76.10'),  # локальный прокси дописал peer в X-Forwarded-For
        (None, None),  # peer неизвестен — отказ, а не доверие заголовкам
    ),
)
def test_resolve_webhook_client_ip_trust_rules(remote: str | None, expected: str | None) -> None:
    ip_object = resolve_webhook_client_ip(remote, forwarded_for=ALLOWED_IP, cf_connecting_ip=ALLOWED_IP)

    assert (str(ip_object) if ip_object is not None else None) == expected


def test_resolve_webhook_client_ip_prefers_last_forwarded_candidate() -> None:
    ip_object = resolve_webhook_client_ip('10.0.0.5', forwarded_for='185.71.76.10, 8.8.8.8')

    assert ip_object is not None
    assert str(ip_object) == '8.8.8.8'


def test_resolve_webhook_client_ip_accepts_allowed_last_forwarded_candidate() -> None:
    ip_object = resolve_webhook_client_ip('10.0.0.5', forwarded_for=f'8.8.8.8, {ALLOWED_IP}')

    assert ip_object is not None
    assert str(ip_object) == ALLOWED_IP


def test_resolve_webhook_client_ip_skips_trusted_proxy_hops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_TRUSTED_PROXY_NETWORKS', '203.0.113.0/24', raising=False)

    ip_object = resolve_webhook_client_ip('10.0.0.5', forwarded_for=f'{ALLOWED_IP}, 203.0.113.10')

    assert ip_object is not None
    assert str(ip_object) == ALLOWED_IP


def test_resolve_webhook_client_ip_trusted_public_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_TRUSTED_PROXY_NETWORKS', '198.51.100.0/24', raising=False)

    ip_object = resolve_webhook_client_ip('198.51.100.20', forwarded_for=f'{ALLOWED_IP}, 198.51.100.10')

    assert ip_object is not None
    assert str(ip_object) == ALLOWED_IP


def test_resolve_webhook_client_ip_ignores_cf_connecting_ip_behind_local_proxy() -> None:
    """Cf-Connecting-Ip за локальным прокси приходит от клиента как есть и не читается."""
    ip_object = resolve_webhook_client_ip('172.18.0.5', forwarded_for='8.8.8.8', cf_connecting_ip=ALLOWED_IP)

    assert ip_object is not None
    assert str(ip_object) == '8.8.8.8'


def test_resolve_webhook_client_ip_trusts_cf_connecting_ip_only_from_cloudflare_peer() -> None:
    ip_object = resolve_webhook_client_ip('104.16.1.1', forwarded_for='8.8.8.8', cf_connecting_ip=ALLOWED_IP)

    assert ip_object is not None
    assert str(ip_object) == ALLOWED_IP


def test_resolve_webhook_client_ip_walks_cloudflare_then_local_proxy_chain() -> None:
    """Cloudflare → Caddy → бот: Caddy дописывает адрес узла Cloudflare, тот доверенный хоп."""
    ip_object = resolve_webhook_client_ip(
        '172.18.0.5', forwarded_for=f'{ALLOWED_IP}, 104.16.1.1', cf_connecting_ip='9.9.9.9'
    )

    assert ip_object is not None
    assert str(ip_object) == ALLOWED_IP


def test_resolve_webhook_client_ip_uses_x_real_ip_only_without_forwarded_for() -> None:
    assert str(resolve_webhook_client_ip('10.0.0.5', real_ip=ALLOWED_IP)) == ALLOWED_IP
    assert str(resolve_webhook_client_ip('10.0.0.5', forwarded_for='8.8.8.8', real_ip=ALLOWED_IP)) == '8.8.8.8'


def test_resolve_webhook_client_ip_returns_none_when_no_candidates() -> None:
    assert resolve_webhook_client_ip(None) is None
    assert resolve_webhook_client_ip('10.0.0.5') is None
    assert resolve_webhook_client_ip('10.0.0.5', forwarded_for='10.0.0.1, 192.168.1.1') is None


async def _post_webhook(client: TestClient, payload: dict, **headers: str) -> web.Response:
    body = json.dumps(payload, ensure_ascii=False)
    return await client.post(
        settings.YOOKASSA_WEBHOOK_PATH,
        data=body.encode('utf-8'),
        headers=_build_headers(**headers),
    )


def _patch_get_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock AsyncSessionLocal used by the webhook handler."""
    from unittest.mock import MagicMock

    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.rollback = AsyncMock()
    mock_session.execute = AsyncMock()

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr('app.external.yookassa_webhook.AsyncSessionLocal', lambda: ctx)


@pytest.mark.asyncio
async def test_handle_webhook_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get_db(monkeypatch)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    app = create_yookassa_webhook_app(service)
    async with TestClient(TestServer(app)) as client:
        payload = {'event': 'payment.succeeded'}
        body = json.dumps(payload, ensure_ascii=False)
        response = await client.post(
            settings.YOOKASSA_WEBHOOK_PATH,
            data=body.encode('utf-8'),
            headers=_build_headers(),
        )
        status = response.status
        text = await response.text()

    assert status == 400
    assert text == 'No payment id'
    process_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_webhook_rejects_cf_connecting_ip_alone_behind_local_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Peer — loopback (тестовый сервер), Cf-Connecting-Ip без X-Forwarded-For: заголовок
    клиентский, Cloudflare его не ставил — отправитель неизвестен, отказ."""
    _patch_get_db(monkeypatch)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    app = create_yookassa_webhook_app(service)
    async with TestClient(TestServer(app)) as client:
        payload = {'event': 'payment.succeeded'}
        body = json.dumps(payload, ensure_ascii=False)
        headers = _build_headers()
        headers.pop('X-Forwarded-For')
        response = await client.post(
            settings.YOOKASSA_WEBHOOK_PATH,
            data=body.encode('utf-8'),
            headers=headers,
        )
        status = response.status

    assert status == 403
    process_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_webhook_with_optional_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get_db(monkeypatch)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    app = create_yookassa_webhook_app(service)
    async with TestClient(TestServer(app)) as client:
        payload = {'event': 'payment.succeeded'}
        body = json.dumps(payload, ensure_ascii=False)
        response = await client.post(
            settings.YOOKASSA_WEBHOOK_PATH,
            data=body.encode('utf-8'),
            headers=_build_headers(Signature='test-signature'),
        )
        status = response.status
        text = await response.text()

    assert status == 400
    assert text == 'No payment id'
    process_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_webhook_accepts_canceled_event(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get_db(monkeypatch)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    app = create_yookassa_webhook_app(service)
    async with TestClient(TestServer(app)) as client:
        payload = {'event': 'payment.canceled', 'object': {'id': 'yk_1'}}
        response = await client.post(
            settings.YOOKASSA_WEBHOOK_PATH,
            data=json.dumps(payload).encode('utf-8'),
            headers=_build_headers(),
        )

        status = response.status

    assert status == 200
    process_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_webhook_rejects_non_yookassa_ip_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get_db(monkeypatch)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    app = create_yookassa_webhook_app(service)
    async with TestClient(TestServer(app)) as client:
        payload = {'event': 'payment.canceled', 'object': {'id': 'yk_x'}}
        response = await client.post(
            settings.YOOKASSA_WEBHOOK_PATH,
            data=json.dumps(payload).encode('utf-8'),
            headers=_build_headers(**{'X-Forwarded-For': '8.8.8.8', 'Cf-Connecting-Ip': '8.8.8.8'}),
        )
        status = response.status
        text = await response.text()

    assert status == 403
    assert text == 'Forbidden'
    process_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_webhook_skip_ip_check_bypasses_ip_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_SKIP_IP_CHECK', True, raising=False)
    _patch_get_db(monkeypatch)

    process_mock = AsyncMock(return_value=True)
    service = SimpleNamespace(process_yookassa_webhook=process_mock)

    app = create_yookassa_webhook_app(service)
    async with TestClient(TestServer(app)) as client:
        # Тот же не-YooKassa IP, что выше даёт 403 — с флагом гейт пропускается.
        payload = {'event': 'payment.canceled', 'object': {'id': 'yk_skip'}}
        response = await client.post(
            settings.YOOKASSA_WEBHOOK_PATH,
            data=json.dumps(payload).encode('utf-8'),
            headers=_build_headers(**{'X-Forwarded-For': '8.8.8.8', 'Cf-Connecting-Ip': '8.8.8.8'}),
        )
        status = response.status

    assert status == 200
    process_mock.assert_awaited_once()
