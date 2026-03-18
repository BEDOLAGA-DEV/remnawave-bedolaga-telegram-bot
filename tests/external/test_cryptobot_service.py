"""Тесты для внешнего клиента CryptoBotService."""

from __future__ import annotations

import hashlib
import hmac
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings
from app.external.cryptobot import CryptoBotService


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


def _enable_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CRYPTOBOT_API_TOKEN', 'token', raising=False)
    monkeypatch.setattr(type(settings), 'get_cryptobot_base_url', lambda self: 'https://cryptobot.test', raising=False)


@pytest.mark.anyio('asyncio')
async def test_create_invoice_uses_make_request(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_token(monkeypatch)
    service = CryptoBotService()

    captured: dict[str, Any] = {}

    async def fake_make_request(method: str, endpoint: str, data: dict[str, Any] | None = None):
        captured['method'] = method
        captured['endpoint'] = endpoint
        captured['data'] = data
        return {'invoice_id': 1}

    monkeypatch.setattr(service, '_make_request', fake_make_request, raising=False)

    result = await service.create_invoice(
        amount='10.00',
        asset='USDT',
        description='Пополнение',
        payload='payload',
        expires_in=600,
    )

    assert result == {'invoice_id': 1}
    assert captured['method'] == 'POST'
    assert captured['endpoint'] == 'createInvoice'
    assert captured['data']['amount'] == '10.00'
    assert captured['data']['payload'] == 'payload'


@pytest.mark.anyio('asyncio')
async def test_make_request_returns_none_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CRYPTOBOT_API_TOKEN', '', raising=False)
    service = CryptoBotService()
    result = await service._make_request('GET', 'getMe')
    assert result is None


def test_verify_webhook_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CRYPTOBOT_API_TOKEN', 'supersecret', raising=False)
    service = CryptoBotService()

    body = '{"invoice_id":1}'
    secret_hash = hashlib.sha256(b'supersecret').digest()
    signature = hmac.new(secret_hash, body.encode(), hashlib.sha256).hexdigest()

    assert service.verify_webhook_signature(body, signature) is True
    assert service.verify_webhook_signature(body, 'invalid') is False


def test_verify_webhook_signature_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CRYPTOBOT_API_TOKEN', '', raising=False)
    service = CryptoBotService()
    assert service.verify_webhook_signature('{}', 'anything') is True
