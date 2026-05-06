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


import urllib.parse

import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def app_client(monkeypatch):
    """Bring up the cabinet FastAPI app with OIDC settings stubbed in."""
    from app.webserver.unified_app import create_app
    from app.cabinet.routes import auth as auth_routes

    async def _enabled(db, key):
        return {
            'TELEGRAM_OIDC_ENABLED': 'true',
            'TELEGRAM_OIDC_CLIENT_ID': '111222333',
            'TELEGRAM_OIDC_CLIENT_SECRET': 'secret',
            'TELEGRAM_OIDC_REDIRECT_URI': 'https://cabinet.example.com/auth/telegram/callback',
        }.get(key)

    async def _no_rate_limit(*a, **kw):
        return False

    monkeypatch.setattr(auth_routes, 'get_setting_value', _enabled)
    monkeypatch.setattr(auth_routes.RateLimitCache, 'is_ip_rate_limited', staticmethod(_no_rate_limit))

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        yield client


@pytest.mark.asyncio
async def test_oidc_init_login_returns_authorize_url(app_client, monkeypatch):
    captured: dict = {}

    async def _gen_state(provider, extra_data=None):
        captured['provider'] = provider
        captured['extra_data'] = extra_data
        return 'S' * 64

    from app.cabinet.routes import auth as auth_routes
    monkeypatch.setattr(auth_routes, 'generate_oauth_state', _gen_state)

    response = await app_client.post('/cabinet/auth/telegram/oidc/init', json={'mode': 'login'})
    assert response.status_code == 200
    body = response.json()
    assert body['state'] == 'S' * 64
    assert body['expires_in'] == 600

    parsed = urllib.parse.urlparse(body['authorize_url'])
    qs = dict(urllib.parse.parse_qsl(parsed.query))
    assert parsed.netloc == 'oauth.telegram.org'
    assert parsed.path == '/auth'
    assert qs['response_type'] == 'code'
    assert qs['scope'] == 'openid profile'
    assert qs['code_challenge_method'] == 'S256'
    assert qs['client_id'] == '111222333'
    assert qs['redirect_uri'] == 'https://cabinet.example.com/auth/telegram/callback'
    assert qs['state'] == 'S' * 64
    assert qs['code_challenge']
    assert qs['nonce']

    assert captured['provider'] == 'telegram'
    assert captured['extra_data']['flow'] == 'login'
    assert captured['extra_data']['code_verifier']
    assert captured['extra_data']['nonce']


@pytest.mark.asyncio
async def test_oidc_init_disabled_returns_400(app_client, monkeypatch):
    async def _disabled(db, key):
        return 'false' if key == 'TELEGRAM_OIDC_ENABLED' else None

    from app.cabinet.routes import auth as auth_routes
    monkeypatch.setattr(auth_routes, 'get_setting_value', _disabled)

    response = await app_client.post('/cabinet/auth/telegram/oidc/init', json={'mode': 'login'})
    assert response.status_code == 400
    assert 'not configured' in response.json()['detail'].lower()


@pytest.mark.asyncio
async def test_oidc_init_link_requires_jwt(app_client):
    response = await app_client.post('/cabinet/auth/telegram/oidc/init', json={'mode': 'link'})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_oidc_init_no_redirect_uri_returns_400(app_client, monkeypatch):
    async def _missing_redirect(db, key):
        return {'TELEGRAM_OIDC_ENABLED': 'true', 'TELEGRAM_OIDC_CLIENT_ID': '111'}.get(key, '')

    from app.cabinet.routes import auth as auth_routes
    monkeypatch.setattr(auth_routes, 'get_setting_value', _missing_redirect)

    response = await app_client.post('/cabinet/auth/telegram/oidc/init', json={'mode': 'login'})
    assert response.status_code == 400
    assert 'redirect uri' in response.json()['detail'].lower()


@pytest.mark.asyncio
async def test_oidc_init_link_with_invalid_jwt_returns_401(app_client):
    """Invalid Bearer token must yield 401, not 500."""
    response = await app_client.post(
        '/cabinet/auth/telegram/oidc/init',
        json={'mode': 'link'},
        headers={'authorization': 'Bearer not-a-real-jwt'},
    )
    # Reject with 401 - not 500 (AttributeError) and not 403 (HTTPBearer auto_error)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_oidc_callback_invalid_state(app_client, monkeypatch):
    async def _no_state(state, provider):
        return None

    from app.cabinet.routes import auth as auth_routes
    monkeypatch.setattr(auth_routes, 'validate_oauth_state', _no_state)

    response = await app_client.post(
        '/cabinet/auth/telegram/oidc/callback',
        json={'code': 'abc', 'state': 'X' * 64},
    )
    assert response.status_code == 400
    assert 'state' in response.json()['detail'].lower()


@pytest.mark.asyncio
async def test_oidc_callback_login_success(app_client, monkeypatch, make_id_token, jwks_doc):
    from datetime import datetime, UTC

    from app.cabinet.routes import auth as auth_routes
    from app.cabinet.auth import telegram_auth
    from app.cabinet.schemas.auth import AuthResponse, UserResponse

    state_data = {
        'provider': 'telegram',
        'flow': 'login',
        'code_verifier': 'verifier_xyz',
        'nonce': 'nonce_xyz',
    }

    async def _validate_state(state, provider):
        return state_data

    async def _exchange(**kwargs):
        return make_id_token(client_id='111222333', nonce='nonce_xyz')

    async def _fake_get_jwks(force=False):
        return jwks_doc

    async def _no_replay(token_hash, ttl):
        return False

    async def _create_or_login(db, claims, *, campaign_slug, referral_code):
        return AuthResponse(
            access_token='access',
            refresh_token='refresh',
            token_type='bearer',
            expires_in=3600,
            user=UserResponse(
                id=1,
                telegram_id=int(claims.get('id') or claims.get('sub')),
                created_at=datetime.now(UTC),
            ),
        )

    monkeypatch.setattr(auth_routes, 'validate_oauth_state', _validate_state)
    monkeypatch.setattr(auth_routes, 'exchange_authorization_code', _exchange)
    monkeypatch.setattr(telegram_auth, '_get_jwks', _fake_get_jwks)
    monkeypatch.setattr(auth_routes.TokenReplayCache, 'is_token_replayed', staticmethod(_no_replay))
    monkeypatch.setattr(auth_routes, '_create_or_login_user_from_oidc_claims', _create_or_login)

    response = await app_client.post(
        '/cabinet/auth/telegram/oidc/callback',
        json={'code': 'auth_code', 'state': 'S' * 64},
    )
    assert response.status_code == 200
    body = response.json()
    assert body['access_token'] == 'access'
    assert body['user']['telegram_id'] == 1234567890


@pytest.mark.asyncio
async def test_oidc_callback_token_exchange_failure(app_client, monkeypatch):
    state_data = {'provider': 'telegram', 'flow': 'login', 'code_verifier': 'v', 'nonce': 'n'}

    async def _validate_state(state, provider):
        return state_data

    async def _exchange(**kwargs):
        return None

    from app.cabinet.routes import auth as auth_routes
    monkeypatch.setattr(auth_routes, 'validate_oauth_state', _validate_state)
    monkeypatch.setattr(auth_routes, 'exchange_authorization_code', _exchange)

    response = await app_client.post(
        '/cabinet/auth/telegram/oidc/callback',
        json={'code': 'c', 'state': 'S' * 64},
    )
    assert response.status_code == 502
    assert 'token exchange' in response.json()['detail'].lower()


@pytest.mark.asyncio
async def test_oidc_callback_nonce_mismatch(app_client, monkeypatch, make_id_token, jwks_doc):
    state_data = {'provider': 'telegram', 'flow': 'login', 'code_verifier': 'v', 'nonce': 'expected_nonce'}

    async def _validate_state(state, provider):
        return state_data

    async def _exchange(**kwargs):
        return make_id_token(client_id='111222333', nonce='different_nonce')

    async def _fake_get_jwks(force=False):
        return jwks_doc

    from app.cabinet.routes import auth as auth_routes
    from app.cabinet.auth import telegram_auth
    monkeypatch.setattr(auth_routes, 'validate_oauth_state', _validate_state)
    monkeypatch.setattr(auth_routes, 'exchange_authorization_code', _exchange)
    monkeypatch.setattr(telegram_auth, '_get_jwks', _fake_get_jwks)

    response = await app_client.post(
        '/cabinet/auth/telegram/oidc/callback',
        json={'code': 'c', 'state': 'S' * 64},
    )
    assert response.status_code == 401
