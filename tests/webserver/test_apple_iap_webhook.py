"""HTTP contract tests for Apple IAP App Store Server Notifications webhook."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.webserver.apple_iap as apple_iap_webserver
from app.config import settings


def _client_for_service_result(monkeypatch, result: tuple[bool, str]) -> TestClient:
    class FakeNotificationService:
        def __init__(self, *args, **kwargs):
            self.calls: list[tuple[str, bytes]] = []

        async def process_signed_payload(self, signed_payload: str, raw_body: bytes) -> tuple[bool, str]:
            self.calls.append((signed_payload, raw_body))
            return result

    monkeypatch.setattr(apple_iap_webserver, 'AppleIAPNotificationService', FakeNotificationService)

    app = FastAPI()
    app.include_router(apple_iap_webserver.create_apple_iap_router())
    return TestClient(app)


def test_apple_iap_webhook_rejects_unsupported_media_type(monkeypatch) -> None:
    client = _client_for_service_result(monkeypatch, (True, 'processed'))

    response = client.post(settings.APPLE_IAP_WEBHOOK_PATH, content='{}', headers={'content-type': 'text/plain'})

    assert response.status_code == 415
    assert response.json() == {'status': 'error', 'reason': 'unsupported_media_type'}


def test_apple_iap_webhook_rejects_body_larger_than_256kb(monkeypatch) -> None:
    client = _client_for_service_result(monkeypatch, (True, 'processed'))

    response = client.post(
        settings.APPLE_IAP_WEBHOOK_PATH,
        content=b'{' + b'"signedPayload":"' + (b'a' * 256_000) + b'"}',
        headers={'content-type': 'application/json'},
    )

    assert response.status_code == 413
    assert response.json() == {'status': 'error', 'reason': 'body_too_large'}


def test_apple_iap_webhook_maps_invalid_signature_to_403(monkeypatch) -> None:
    client = _client_for_service_result(monkeypatch, (False, 'invalid_signature'))

    response = client.post(settings.APPLE_IAP_WEBHOOK_PATH, json={'signedPayload': 'signed.payload'})

    assert response.status_code == 403
    assert response.json() == {'status': 'error', 'reason': 'invalid_signature'}


def test_apple_iap_webhook_maps_configuration_error_to_503(monkeypatch) -> None:
    client = _client_for_service_result(monkeypatch, (False, 'configuration_error'))

    response = client.post(settings.APPLE_IAP_WEBHOOK_PATH, json={'signedPayload': 'signed.payload'})

    assert response.status_code == 503
    assert response.json() == {'status': 'error', 'reason': 'configuration_error'}


def test_apple_iap_webhook_maps_missing_notification_uuid_to_400(monkeypatch) -> None:
    client = _client_for_service_result(monkeypatch, (False, 'missing_notification_uuid'))

    response = client.post(settings.APPLE_IAP_WEBHOOK_PATH, json={'signedPayload': 'signed.payload'})

    assert response.status_code == 400
    assert response.json() == {'status': 'error', 'reason': 'missing_notification_uuid'}


def test_apple_iap_webhook_maps_signed_transaction_verification_failed_to_400(monkeypatch) -> None:
    client = _client_for_service_result(monkeypatch, (False, 'signed_transaction_verification_failed'))

    response = client.post(settings.APPLE_IAP_WEBHOOK_PATH, json={'signedPayload': 'signed.payload'})

    assert response.status_code == 400
    assert response.json() == {'status': 'error', 'reason': 'signed_transaction_verification_failed'}


def test_apple_iap_webhook_returns_ok_for_processed_notification(monkeypatch) -> None:
    client = _client_for_service_result(monkeypatch, (True, 'processed'))

    response = client.post(settings.APPLE_IAP_WEBHOOK_PATH, json={'signedPayload': 'signed.payload'})

    assert response.status_code == 200
    assert response.json() == {'status': 'ok', 'reason': 'processed'}


def test_apple_iap_health_returns_503_when_feature_enabled_but_not_configured(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'APPLE_IAP_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_ROOT_CERTS_PATHS', '', raising=False)

    app = FastAPI()
    app.include_router(apple_iap_webserver.create_apple_iap_router())
    response = TestClient(app).get('/health/apple-iap')

    assert response.status_code == 503
    assert response.json()['status'] == 'configuration_error'
    assert response.json()['enabled'] is False


def test_payment_router_mounts_apple_webhook_when_enabled_but_misconfigured(monkeypatch) -> None:
    from unittest.mock import MagicMock

    from app.webserver.payments import create_payment_router

    monkeypatch.setattr(settings, 'APPLE_IAP_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_ROOT_CERTS_PATHS', '', raising=False)

    router = create_payment_router(MagicMock(), MagicMock())

    assert router is not None
    paths = {route.path for route in router.routes}
    assert settings.APPLE_IAP_WEBHOOK_PATH in paths
    assert '/health/apple-iap' in paths
