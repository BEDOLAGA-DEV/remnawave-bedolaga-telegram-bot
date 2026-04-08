import hashlib
import hmac
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.config import settings
from app.services.remnawave_webhook_service import RemnaWaveWebhookService
from app.webserver.remnawave_webhook import create_remnawave_webhook_router


@pytest.fixture(autouse=True)
def reset_remnawave_webhook_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'REMNAWAVE_WEBHOOK_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'REMNAWAVE_WEBHOOK_PATH', '/remnawave-webhook', raising=False)
    monkeypatch.setattr(
        settings,
        'REMNAWAVE_WEBHOOK_SECRET',
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
        raising=False,
    )
    RemnaWaveWebhookService._intentional_panel_deletions_by_uuid.clear()
    RemnaWaveWebhookService._intentional_panel_deletions_by_telegram_id.clear()


def _get_route(router, path: str, method: str = 'POST'):
    for route in router.routes:
        if getattr(route, 'path', '') == path and method in getattr(route, 'methods', set()):
            return route
    raise AssertionError(f'Route {path} with method {method} not found')


def _build_request(path: str, body: bytes, headers: dict[str, str] | None = None) -> Request:
    scope = {
        'type': 'http',
        'asgi': {'version': '3.0'},
        'method': 'POST',
        'path': path,
        'headers': [(k.lower().encode('latin-1'), v.encode('latin-1')) for k, v in (headers or {}).items()],
    }

    async def receive() -> dict[str, Any]:
        return {'type': 'http.request', 'body': body, 'more_body': False}

    return Request(scope, receive)


def _signature(body: bytes) -> str:
    secret = settings.REMNAWAVE_WEBHOOK_SECRET or ''
    return hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()


@pytest.mark.anyio('asyncio')
async def test_remnawave_webhook_accepts_event_without_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = AsyncMock()
    process_event = AsyncMock(return_value=True)
    service = SimpleNamespace(
        process_event=process_event,
        is_admin_event=lambda _event_name: True,
    )

    monkeypatch.setattr(
        'app.webserver.remnawave_webhook.RemnaWaveWebhookService',
        lambda _bot: service,
    )

    payload = {
        'event': 'user.modified',
        'data': {'uuid': 'user-123'},
        'timestamp': '2026-03-30T12:00:00.000Z',
    }
    raw_body = json.dumps(payload).encode('utf-8')

    router = create_remnawave_webhook_router(bot)
    path = settings.REMNAWAVE_WEBHOOK_PATH
    route = _get_route(router, path)
    request = _build_request(
        path,
        raw_body,
        headers={'X-Remnawave-Signature': _signature(raw_body)},
    )

    response = await route.endpoint(request)

    assert response.status_code == 200
    process_event.assert_awaited_once_with(None, 'user.modified', {'uuid': 'user-123'})


@pytest.mark.anyio('asyncio')
async def test_remnawave_webhook_rejects_payload_without_event() -> None:
    bot = AsyncMock()
    payload = {'data': {'uuid': 'user-123'}}
    raw_body = json.dumps(payload).encode('utf-8')

    router = create_remnawave_webhook_router(bot)
    path = settings.REMNAWAVE_WEBHOOK_PATH
    route = _get_route(router, path)
    request = _build_request(
        path,
        raw_body,
        headers={'X-Remnawave-Signature': _signature(raw_body)},
    )

    response = await route.endpoint(request)

    assert response.status_code == 400
    assert json.loads(response.body.decode('utf-8')) == {'status': 'error', 'reason': 'missing_event'}


def test_intentional_panel_deletion_guard_marks_and_detects() -> None:
    """Verify that mark + is_intentional round-trip works correctly."""
    RemnaWaveWebhookService.mark_intentional_panel_deletion(
        panel_uuids=['panel-user-123'],
        telegram_id=8368498066,
    )

    assert RemnaWaveWebhookService._is_intentional_panel_deletion_event(
        {'uuid': 'panel-user-123', 'telegramId': 8368498066}
    )

    # Unknown UUID should not match
    assert not RemnaWaveWebhookService._is_intentional_panel_deletion_event(
        {'uuid': 'unknown-uuid', 'telegramId': 99999}
    )


def test_intentional_panel_deletion_guard_respects_hard_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that the guard stops accepting entries after hitting the cap."""
    monkeypatch.setattr(RemnaWaveWebhookService, '_MAX_INTENTIONAL_ENTRIES', 3)

    RemnaWaveWebhookService.mark_intentional_panel_deletion(panel_uuids=['a', 'b', 'c'])
    # 3 entries — at capacity
    RemnaWaveWebhookService.mark_intentional_panel_deletion(panel_uuids=['d'])
    # 'd' should NOT be stored (cap reached)
    assert 'd' not in RemnaWaveWebhookService._intentional_panel_deletions_by_uuid
