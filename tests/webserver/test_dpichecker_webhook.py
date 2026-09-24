"""Вебхук DPI//CHECKER: подпись HMAC по сырому телу (при несовпадении секрет перечитывается один раз),
monitor.run будит обходчик, завершение ручной проверки обновляет строку без уведомления,
повтор доставки и чужие номера — тихо 200, слишком большое тело — 413."""

from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.webserver.dpichecker_webhook import MAX_BODY_BYTES, create_dpichecker_webhook_router
from tests.fixtures.dpichecker_fixtures import load_dpichecker_fixture


SECRET = 'whsec_test'


class FakeService:
    def __init__(self, secrets=(SECRET,)):
        self._secrets = list(secrets)
        self.secret_calls: list[bool] = []
        self.events: list[tuple] = []

    async def webhook_secret(self, *, refresh=False):
        self.secret_calls.append(refresh)
        return self._secrets[min(len(self.secret_calls) - 1, len(self._secrets) - 1)]

    async def handle_webhook(self, *, event, delivery_id, payload):
        self.events.append((event, delivery_id))


def _client(service) -> TestClient:
    app = FastAPI()
    app.include_router(create_dpichecker_webhook_router(service))
    return TestClient(app)


def _post(client, name: str, *, secret: str = SECRET, event: str | None = None):
    body = json.dumps(load_dpichecker_fixture(name)['body']).encode()
    signature = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    payload = json.loads(body)
    headers = {
        'X-DPIChecker-Signature': signature,
        'X-DPIChecker-Event': event or payload['event'],
        'X-DPIChecker-Delivery': str(payload['delivery_id']),
        'Content-Type': 'application/json',
    }
    return client.post('/dpichecker/webhook', content=body, headers=headers)


def test_good_signature_dispatches_event():
    service = FakeService()
    response = _post(_client(service), 'webhook_monitor_run')
    assert response.status_code == 200
    assert service.events == [('monitor.run', 6)]


def test_bad_signature_rejected_after_one_secret_refresh():
    service = FakeService()
    response = _post(_client(service), 'webhook_check_completed', secret='wrong')
    assert response.status_code == 401
    assert service.secret_calls == [False, True]
    assert service.events == []


def test_rotated_secret_accepted_after_refresh():
    service = FakeService(secrets=('old', SECRET))
    response = _post(_client(service), 'webhook_check_completed')
    assert response.status_code == 200 and service.secret_calls == [False, True]


def test_missing_signature_401():
    service = FakeService()
    response = _client(service).post('/dpichecker/webhook', content=b'{}', headers={'X-DPIChecker-Event': 'x'})
    assert response.status_code == 401 and service.events == []


def test_empty_body_400():
    assert _client(FakeService()).post('/dpichecker/webhook', content=b'').status_code == 400


def test_oversized_body_413():
    response = _client(FakeService()).post('/dpichecker/webhook', content=b'x' * (MAX_BODY_BYTES + 1))
    assert response.status_code == 413
