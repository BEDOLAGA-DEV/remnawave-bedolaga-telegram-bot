import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, status

from app.config import settings
from app.services.payment_service import PaymentService


# ensure backup directory exists before importing the unified app to avoid side effects during module import
_backup_dir = Path('data/backups')
_backup_dir.mkdir(parents=True, exist_ok=True)

from app.webserver.unified_app import create_unified_app


@pytest.mark.anyio
async def test_unified_app_health_reports_features(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bot = AsyncMock()
    dispatcher = SimpleNamespace(feed_update=AsyncMock())
    payment_service = AsyncMock(spec=PaymentService)

    miniapp_static_dir = tmp_path / 'miniapp'
    miniapp_static_dir.mkdir()

    monkeypatch.setattr(settings, 'WEB_API_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'TRIBUTE_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'WEBHOOK_URL', 'https://hooks.example.com', raising=False)
    monkeypatch.setattr(settings, 'WEBHOOK_PATH', '/telegram-webhook', raising=False)
    monkeypatch.setattr(settings, 'WEBHOOK_SECRET_TOKEN', 'super-secret', raising=False)
    monkeypatch.setattr(settings, 'BOT_RUN_MODE', 'webhook', raising=False)
    monkeypatch.setattr(settings, 'WEBHOOK_MAX_QUEUE_SIZE', 8, raising=False)
    monkeypatch.setattr(settings, 'WEBHOOK_WORKERS', 1, raising=False)
    monkeypatch.setattr(settings, 'WEBHOOK_ENQUEUE_TIMEOUT', 0.0, raising=False)
    monkeypatch.setattr(settings, 'WEBHOOK_WORKER_SHUTDOWN_TIMEOUT', 1.0, raising=False)
    monkeypatch.setattr(settings, 'MINIAPP_STATIC_PATH', str(miniapp_static_dir), raising=False)
    monkeypatch.setattr(settings, 'BACKUP_LOCATION', str(tmp_path / 'backups'), raising=False)

    app = create_unified_app(
        bot,
        dispatcher,  # type: ignore[arg-type]
        payment_service,
        enable_telegram_webhook=True,
    )

    health_route = next(
        route
        for route in app.routes
        if getattr(route, 'endpoint', None) and getattr(route.endpoint, '__name__', '') == 'unified_health'
    )

    assert getattr(health_route, 'path', None) == '/health/unified'

    # FastAPI lifespan заменил deprecated @app.on_event — startup/shutdown
    # хуки регистрируются через async-контекст. router.startup()/shutdown()
    # legacy on_event'ов больше не существуют.
    async with app.router.lifespan_context(app):
        response = await health_route.endpoint()  # type: ignore[func-returns-value]

    payload = json.loads(response.body.decode('utf-8'))  # type: ignore[attr-defined]

    assert payload['status'] == 'ok'
    assert payload['web_api_enabled'] is True
    assert payload['bot_run_mode'] == 'webhook'
    assert payload['telegram_webhook']['enabled'] is True
    assert payload['telegram_webhook']['running'] is True
    assert payload['telegram_webhook']['path'] == '/telegram-webhook'
    assert payload['telegram_webhook']['secret_configured'] is True
    assert payload['payment_webhooks']['enabled'] is True
    assert payload['payment_webhooks']['providers']['tribute'] is True
    assert payload['miniapp_static']['mounted'] is True
    assert payload['miniapp_static']['path'].endswith('miniapp')


def _build_unified_app(monkeypatch: pytest.MonkeyPatch, docs_enabled: bool) -> FastAPI:
    bot = AsyncMock()
    dispatcher = SimpleNamespace(feed_update=AsyncMock())
    payment_service = AsyncMock(spec=PaymentService)

    monkeypatch.setattr(settings, 'WEB_API_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'WEB_API_DOCS_ENABLED', docs_enabled, raising=False)
    monkeypatch.setattr(settings, 'MINIAPP_STATIC_PATH', 'miniapp', raising=False)

    return create_unified_app(
        bot,
        dispatcher,  # type: ignore[arg-type]
        payment_service,
        enable_telegram_webhook=False,
    )


def test_unified_app_apple_iap_only_mounts_only_apple_cabinet_routes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bot = AsyncMock()
    dispatcher = SimpleNamespace(feed_update=AsyncMock())
    payment_service = AsyncMock(spec=PaymentService)
    cert_path = tmp_path / 'apple-root.cer'
    cert_path.write_bytes(b'dummy-cert')

    monkeypatch.setattr(settings, 'WEB_API_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'CABINET_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'MINIAPP_STATIC_PATH', str(tmp_path / 'missing-miniapp'), raising=False)
    monkeypatch.setattr(settings, 'MEDIA_UPLOAD_DIR', str(tmp_path / 'uploads'), raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_KEY_ID', 'TEST_KEY_ID', raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_ISSUER_ID', 'test-issuer-id', raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_BUNDLE_ID', 'com.org.app', raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_APP_APPLE_ID', 123456789, raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_ENVIRONMENT', 'Sandbox', raising=False)
    monkeypatch.setattr(
        settings, 'APPLE_IAP_PRIVATE_KEY', '-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----', raising=False
    )
    monkeypatch.setattr(settings, 'APPLE_IAP_PRIVATE_KEY_PATH', None, raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_ROOT_CERTS_PATHS', str(cert_path), raising=False)

    app = create_unified_app(
        bot,
        dispatcher,  # type: ignore[arg-type]
        payment_service,
        enable_telegram_webhook=False,
    )

    registered_paths = {getattr(route, 'path', None) for route in app.routes}

    assert '/cabinet/apple-iap/account-token' in registered_paths
    assert '/cabinet/apple-purchase' in registered_paths
    assert '/cabinet/admin/apple-iap/transactions' in registered_paths
    assert '/cabinet/subscription' not in registered_paths
    assert '/cabinet/balance' not in registered_paths
    assert '/cabinet/admin/users' not in registered_paths


@pytest.mark.anyio
async def test_unified_app_health_path_without_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = AsyncMock()
    dispatcher = SimpleNamespace(feed_update=AsyncMock())
    payment_service = AsyncMock(spec=PaymentService)

    monkeypatch.setattr(settings, 'WEB_API_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'MINIAPP_STATIC_PATH', 'miniapp', raising=False)

    app = create_unified_app(
        bot,
        dispatcher,  # type: ignore[arg-type]
        payment_service,
        enable_telegram_webhook=False,
    )

    health_route = next(
        route
        for route in app.routes
        if getattr(route, 'endpoint', None) and getattr(route.endpoint, '__name__', '') == 'unified_health'
    )

    assert getattr(health_route, 'path', None) == '/health'


def test_unified_app_docs_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_unified_app(monkeypatch, docs_enabled=False)

    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None

    registered_paths = {getattr(route, 'path', None) for route in app.routes}
    assert '/doc' not in registered_paths


@pytest.mark.anyio
async def test_unified_app_docs_enabled_with_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_unified_app(monkeypatch, docs_enabled=True)

    assert app.docs_url == '/docs'
    assert app.openapi_url == '/openapi.json'

    alias_route = next(
        (route for route in app.routes if getattr(route, 'path', None) == '/doc'),
        None,
    )
    assert alias_route is not None
    assert getattr(alias_route, 'include_in_schema', True) is False

    redoc_route = next(
        (route for route in app.routes if getattr(route, 'path', None) == '/redoc'),
        None,
    )
    assert redoc_route is not None
    assert getattr(redoc_route, 'include_in_schema', True) is False

    response = await alias_route.endpoint()  # type: ignore[func-returns-value]
    assert response.status_code == status.HTTP_307_TEMPORARY_REDIRECT
    assert response.headers['location'] == '/docs'

    redoc_response = await redoc_route.endpoint()  # type: ignore[func-returns-value]
    assert b'ReDoc' in redoc_response.body  # type: ignore[attr-defined]
