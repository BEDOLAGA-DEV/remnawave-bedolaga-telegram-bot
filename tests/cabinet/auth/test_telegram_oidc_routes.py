"""Schema and route tests for the Telegram OIDC rewrite."""

import pytest
from pydantic import ValidationError


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
