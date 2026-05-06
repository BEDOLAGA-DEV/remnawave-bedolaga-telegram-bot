"""Schema and route tests for the Telegram OIDC rewrite."""

import sys
import types

import pytest
from pydantic import ValidationError


# Extend the conftest redis stub: app.utils.cache imports `from redis.exceptions
# import NoScriptError`, which the conftest stub doesn't provide. Add a minimal
# `redis.exceptions` shim so tests that import route modules can load cache.py.
if 'redis' in sys.modules and not hasattr(sys.modules['redis'], 'exceptions'):
    _redis_mod = sys.modules['redis']
    _redis_exc = types.ModuleType('redis.exceptions')

    class _NoScriptError(Exception):
        pass

    _redis_exc.NoScriptError = _NoScriptError
    _redis_mod.exceptions = _redis_exc
    sys.modules['redis.exceptions'] = _redis_exc


def test_init_request_login_mode_no_jwt():
    from app.cabinet.schemas.auth import TelegramOIDCInitRequest
    req = TelegramOIDCInitRequest(mode='login')
    assert req.mode == 'login'
    assert req.campaign_slug is None


def test_init_request_link_mode():
    from app.cabinet.schemas.auth import TelegramOIDCInitRequest
    req = TelegramOIDCInitRequest(mode='link')
    assert req.mode == 'link'


def test_init_request_invalid_mode_rejected():
    from app.cabinet.schemas.auth import TelegramOIDCInitRequest
    with pytest.raises(ValidationError):
        TelegramOIDCInitRequest(mode='register')


def test_init_request_referral_pattern():
    from app.cabinet.schemas.auth import TelegramOIDCInitRequest
    with pytest.raises(ValidationError):
        TelegramOIDCInitRequest(mode='login', referral_code='!!bad!!')


def test_init_response_shape():
    from app.cabinet.schemas.auth import TelegramOIDCInitResponse
    resp = TelegramOIDCInitResponse(
        authorize_url='https://oauth.telegram.org/auth?...',
        state='S' * 64,
        expires_in=600,
    )
    assert resp.expires_in == 600


def test_callback_request_shape():
    from app.cabinet.schemas.auth import TelegramOIDCCallbackRequest
    req = TelegramOIDCCallbackRequest(code='abc', state='S' * 64)
    assert req.code == 'abc'


def test_oidc_auth_request_optional_nonce():
    from app.cabinet.schemas.auth import TelegramOIDCAuthRequest
    req = TelegramOIDCAuthRequest(id_token='x.y.z', nonce='abc123def456')
    assert req.nonce == 'abc123def456'

    req2 = TelegramOIDCAuthRequest(id_token='x.y.z')
    assert req2.nonce is None


@pytest.mark.asyncio
async def test_popup_endpoint_passes_nonce_to_validator(monkeypatch):
    """If request.nonce is set, validate_telegram_oidc_token receives expected_nonce."""
    from app.cabinet.routes import auth as auth_routes
    from app.cabinet.schemas.auth import TelegramOIDCAuthRequest

    captured: dict = {}

    async def _fake_validate(id_token, client_id, expected_nonce=None):
        captured['expected_nonce'] = expected_nonce
        return None  # 401 path keeps the test simple

    async def _no_rate_limit(*a, **kw):
        return False

    async def _setting(db, key):
        return {'TELEGRAM_OIDC_ENABLED': 'true', 'TELEGRAM_OIDC_CLIENT_ID': '111'}.get(key)

    monkeypatch.setattr(auth_routes, 'validate_telegram_oidc_token', _fake_validate)
    monkeypatch.setattr(auth_routes.RateLimitCache, 'is_ip_rate_limited', staticmethod(_no_rate_limit))
    monkeypatch.setattr(auth_routes, 'get_setting_value', _setting)

    request_obj = TelegramOIDCAuthRequest(id_token='x.y.z', nonce='nonce-from-frontend')

    class _Req:
        @property
        def headers(self):
            return {}

        client = type('c', (), {'host': '127.0.0.1'})()

    with pytest.raises(Exception):
        await auth_routes.auth_telegram_oidc(request_obj, _Req(), db=None)

    assert captured['expected_nonce'] == 'nonce-from-frontend'
