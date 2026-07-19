"""Contract tests for `app.services.lava_service.LavaService`.

This module has flip-flopped between two incompatible signature schemes:
  * body-embedded `signature` field + canonical (sorted-keys) JSON  — legacy PHP SDK
  * `Signature` HTTP header + HMAC of raw body bytes               — current api.lava.ru

The current Lava Business contract is the HEADER form. These tests pin it so
a future refactor can't quietly flip back without test signal.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.lava_service import LavaAPIError, LavaService


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> LavaService:
    """LavaService with deterministic credentials."""
    from app.config import settings

    monkeypatch.setattr(settings, 'LAVA_SHOP_ID', 'shop-xyz', raising=False)
    monkeypatch.setattr(settings, 'LAVA_SECRET_KEY', 'outgoing-secret', raising=False)
    monkeypatch.setattr(settings, 'LAVA_WEBHOOK_SECRET', 'webhook-secret', raising=False)
    monkeypatch.setattr(settings, 'LAVA_BASE_URL', 'https://api.lava.ru', raising=False)
    return LavaService()


def _hmac_hex(message: str | bytes, key: str) -> str:
    msg = message.encode('utf-8') if isinstance(message, str) else message
    return hmac.new(key.encode('utf-8'), msg, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Outgoing request contract: Signature HTTP header + HMAC of raw body bytes
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal aiohttp.ClientResponse stub."""

    def __init__(self, status: int = 200, payload: dict | None = None) -> None:
        self.status = status
        self._payload = payload or {'status': 'success', 'data': {'id': 'inv_1', 'url': 'https://pay'}}

    async def json(self, content_type: Any = None) -> dict:
        return self._payload

    async def text(self) -> str:
        return json.dumps(self._payload)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *a: object) -> None:
        return None


@pytest.mark.asyncio
async def test_outgoing_signature_is_in_header_not_body(service: LavaService) -> None:
    """REGRESSION: signature must be in `Signature` HTTP header, NOT body field."""
    captured: dict[str, Any] = {}

    fake_session = MagicMock()

    def _post(url: str, data: bytes, headers: dict[str, str]) -> _FakeResponse:
        captured['url'] = url
        captured['data'] = data
        captured['headers'] = headers
        return _FakeResponse()

    fake_session.post = _post
    fake_session.closed = False

    with patch.object(service, '_get_session', AsyncMock(return_value=fake_session)):
        await service.create_invoice(amount_rubles=10.0, order_id='ord-1')

    assert 'Signature' in captured['headers'], 'Signature header missing — Lava will reject with 401'
    sent_body = captured['data']
    # Body must be raw JSON bytes with NO embedded `signature` field.
    parsed = json.loads(sent_body)
    assert 'signature' not in parsed, (
        'signature must NOT be embedded in body — modern api.lava.ru only accepts header form'
    )


@pytest.mark.asyncio
async def test_outgoing_signature_is_hmac_of_raw_body_bytes(service: LavaService) -> None:
    """Signature header value = HMAC-SHA256(raw_body_bytes, LAVA_SECRET_KEY) hex."""
    captured: dict[str, Any] = {}
    fake_session = MagicMock()

    def _post(url: str, data: bytes, headers: dict[str, str]) -> _FakeResponse:
        captured['data'] = data
        captured['headers'] = headers
        return _FakeResponse()

    fake_session.post = _post
    fake_session.closed = False

    with patch.object(service, '_get_session', AsyncMock(return_value=fake_session)):
        await service.create_invoice(amount_rubles=15.5, order_id='ord-2')

    expected = _hmac_hex(captured['data'], 'outgoing-secret')
    assert captured['headers']['Signature'] == expected, (
        'Signature must be HMAC of exact bytes sent — any re-sort would diverge from raw body'
    )


@pytest.mark.asyncio
async def test_outgoing_body_uses_payload_key_order_not_sorted(service: LavaService) -> None:
    """We must NOT sort keys outgoing — sorted body + HMAC of raw would not match."""
    captured: dict[str, Any] = {}
    fake_session = MagicMock()

    def _post(url: str, data: bytes, headers: dict[str, str]) -> _FakeResponse:
        captured['data'] = data
        return _FakeResponse()

    fake_session.post = _post
    fake_session.closed = False

    with patch.object(service, '_get_session', AsyncMock(return_value=fake_session)):
        await service.create_invoice(amount_rubles=10.0, order_id='ord-3', hook_url='https://example.com/hook')

    text = captured['data'].decode('utf-8')
    # `sum` first (as in payload), not `hookUrl` (which alphabetically precedes).
    sum_idx = text.find('"sum"')
    hook_idx = text.find('"hookUrl"')
    assert sum_idx < hook_idx, 'Outgoing body key order changed — would break signature verification on Lava side'


@pytest.mark.asyncio
async def test_http_error_raises_lava_api_error(service: LavaService) -> None:
    """4xx/5xx must surface as LavaAPIError with status and message."""
    fake_session = MagicMock()

    def _post(*_a: Any, **_kw: Any) -> _FakeResponse:
        return _FakeResponse(status=401, payload={'status': 'error', 'error': 'Invalid signature', 'code': 'sig'})

    fake_session.post = _post
    fake_session.closed = False

    with patch.object(service, '_get_session', AsyncMock(return_value=fake_session)):
        with pytest.raises(LavaAPIError) as exc_info:
            await service.create_invoice(amount_rubles=1.0, order_id='ord-err')

    assert exc_info.value.status_code == 401
    assert 'Invalid signature' in str(exc_info.value)


# ---------------------------------------------------------------------------
# Webhook verification: tolerates both raw-body and canonical-JSON signing
# ---------------------------------------------------------------------------


def test_webhook_verify_accepts_raw_body_hmac(service: LavaService) -> None:
    """Modern shops sign raw body — verify must accept."""
    body = b'{"order_id":"x","status":"success"}'
    sig = _hmac_hex(body, 'webhook-secret')

    assert service.verify_webhook_signature(body, sig) is True


def test_webhook_verify_accepts_canonical_json_hmac(service: LavaService) -> None:
    """Legacy PHP-SDK shops sign canonical (sorted-keys) JSON — verify must still accept."""
    # Raw body with NON-sorted keys; canonical re-serializes with sort_keys.
    body = b'{"status":"success","order_id":"x"}'
    canonical = json.dumps({'order_id': 'x', 'status': 'success'}, sort_keys=True, separators=(',', ':'))
    sig = _hmac_hex(canonical, 'webhook-secret')

    assert service.verify_webhook_signature(body, sig) is True


def test_webhook_verify_rejects_unknown_signature(service: LavaService) -> None:
    body = b'{"order_id":"x","status":"success"}'

    assert service.verify_webhook_signature(body, 'deadbeef' * 8) is False


def test_webhook_verify_rejects_empty_signature(service: LavaService) -> None:
    body = b'{"order_id":"x"}'
    assert service.verify_webhook_signature(body, '') is False


def test_webhook_verify_rejects_missing_webhook_secret(service: LavaService, monkeypatch: pytest.MonkeyPatch) -> None:
    """No webhook secret configured → fail closed, not open."""
    from app.config import settings

    monkeypatch.setattr(settings, 'LAVA_WEBHOOK_SECRET', '', raising=False)

    body = b'{"order_id":"x"}'
    sig = _hmac_hex(body, '')  # would match if we computed with empty key — must still fail
    assert service.verify_webhook_signature(body, sig) is False


def test_webhook_verify_handles_garbage_body(service: LavaService) -> None:
    """Non-JSON body falls through to raw-only path, then mismatch → False (no crash)."""
    body = b'\x00\x01not json'
    sig = _hmac_hex(body, 'webhook-secret')
    # Raw matches → accepted (this is correct; Lava could in principle sign anything).
    assert service.verify_webhook_signature(body, sig) is True

    # And clearly-wrong signature → False.
    assert service.verify_webhook_signature(body, 'a' * 64) is False


def test_strip_url_query_removes_query_and_fragment() -> None:
    """Lava Business rejects success/fail URLs with a query string (HTTP 422 'ошибочный
    формат ссылки'). The sanitizer must drop query + fragment but keep the rest intact —
    this is exactly the cabinet top-up regression where ?method=lava&status=success broke
    Lava invoice creation while the bot (which sends no return URL) kept working."""
    from app.services.lava_service import _strip_url_query

    assert (
        _strip_url_query('https://c.example/balance/top-up/result?method=lava&status=success')
        == 'https://c.example/balance/top-up/result'
    )
    assert _strip_url_query('https://c.example/p#frag') == 'https://c.example/p'
    # Clean URLs (including the path-based method variant) pass through unchanged.
    assert _strip_url_query('https://c.example/balance/top-up/result/lava') == (
        'https://c.example/balance/top-up/result/lava'
    )
    assert _strip_url_query('https://c.example/p') == 'https://c.example/p'
