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


def test_apple_iap_webhook_returns_ok_for_processed_notification(monkeypatch) -> None:
    client = _client_for_service_result(monkeypatch, (True, 'processed'))

    response = client.post(settings.APPLE_IAP_WEBHOOK_PATH, json={'signedPayload': 'signed.payload'})

    assert response.status_code == 200
    assert response.json() == {'status': 'ok', 'reason': 'processed'}
