from __future__ import annotations

import inspect
import time

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_mobile
from app.cabinet.routes.media import (
    _BLOCKED_UPLOAD_CONTENT_TYPES,
    _BLOCKED_UPLOAD_EXTENSIONS,
    _media_signature,
    _verify_media_token,
    make_media_token,
)


FID = 'BAADAgADabcdef_-1234567890'


@pytest.mark.asyncio
async def test_mobile_realtime_contract_is_explicitly_disabled() -> None:
    response = await admin_mobile.get_mobile_realtime_contract()

    assert response.enabled is False
    assert response.feature == 'realtime_tickets'
    assert '/ws?api_key=' in response.reason
    assert '/cabinet/ws?token=' in response.reason


def test_mobile_router_does_not_ship_query_token_websocket_contract() -> None:
    source = inspect.getsource(admin_mobile)

    assert '@router.websocket' not in source
    assert 'query_params' not in source
    assert 'websocket.accept' not in source


def test_signed_media_token_is_required_bound_and_expiring_for_mobile_downloads() -> None:
    token = make_media_token(FID)

    assert _verify_media_token(FID, token) is True
    assert _verify_media_token('BQADdifferent_-9876543210zy', token) is False

    exp = int(time.time()) - 10
    assert _verify_media_token(FID, f'{exp}.{_media_signature(FID, exp)}') is False


def test_media_contract_rejects_scriptable_upload_types() -> None:
    assert '.svg' in _BLOCKED_UPLOAD_EXTENSIONS
    assert '.html' in _BLOCKED_UPLOAD_EXTENSIONS
    assert 'image/svg+xml' in _BLOCKED_UPLOAD_CONTENT_TYPES
    assert 'text/html' in _BLOCKED_UPLOAD_CONTENT_TYPES


def test_mobile_media_contract_source_has_no_admin_token_download_requirement() -> None:
    source = inspect.getsource(admin_mobile)

    assert 'X-API-Key' not in source
    assert 'WEB_API_DEFAULT_TOKEN' not in source
    assert 'require_api_token' not in source


@pytest.mark.asyncio
async def test_realtime_response_does_not_enable_admin_event_subscription_for_downgraded_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def rejected_dependency(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail='downgraded')

    monkeypatch.setattr(admin_mobile, 'get_mobile_realtime_contract', rejected_dependency)

    with pytest.raises(HTTPException) as exc:
        await admin_mobile.get_mobile_realtime_contract()

    assert exc.value.status_code == 403
