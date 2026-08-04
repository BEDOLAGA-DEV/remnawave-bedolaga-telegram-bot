from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Self
from unittest.mock import AsyncMock

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.cabinet.auth import oauth_providers
from app.cabinet.auth.oauth_providers import GoogleProvider, validate_google_id_token


class _FakeTokenResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, str]:
        return {'access_token': 'access-token'}


class _FakeAsyncClient:
    last_post_json: ClassVar[dict[str, str] | None] = None

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, _url: str, **kwargs: object) -> _FakeTokenResponse:
        payload = kwargs.get('json')
        assert isinstance(payload, dict)
        self.__class__.last_post_json = payload
        return _FakeTokenResponse()


def _rsa_private_key_pem() -> tuple[str, rsa.RSAPrivateKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return pem, private_key


def _jwk_for_key(private_key: rsa.RSAPrivateKey, kid: str) -> dict[str, str]:
    jwk = json.loads(pyjwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk['kid'] = kid
    jwk['alg'] = 'RS256'
    return jwk


def _google_token(
    private_key_pem: str,
    kid: str,
    *,
    audience: str = 'ios-client',
    issuer: str = 'https://accounts.google.com',
    expires_delta: timedelta = timedelta(minutes=5),
    nonce: str | None = None,
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        'iss': issuer,
        'aud': audience,
        'sub': 'google-sub',
        'iat': int(now.timestamp()),
        'exp': int((now + expires_delta).timestamp()),
        'email': 'alice@example.com',
        'email_verified': True,
        'given_name': 'Alice',
        'family_name': 'Google',
        'picture': 'https://example.com/alice.png',
    }
    if nonce:
        claims['nonce'] = nonce
    return pyjwt.encode(
        claims,
        private_key_pem,
        algorithm='RS256',
        headers={'kid': kid},
    )


def _provider() -> GoogleProvider:
    return GoogleProvider(
        client_id='web-client',
        client_secret='web-secret',
        redirect_uri='https://cabinet.example.com/auth/oauth/callback',
        ios_client_id='ios-client',
        android_client_id='android-client',
    )


@pytest.mark.asyncio
async def test_validate_google_id_token_accepts_ios_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    private_key_pem, private_key = _rsa_private_key_pem()
    kid = 'GOOGLEKID'
    monkeypatch.setattr(
        oauth_providers,
        '_get_google_jwks',
        AsyncMock(return_value={'keys': [_jwk_for_key(private_key, kid)]}),
    )

    claims = await validate_google_id_token(_google_token(private_key_pem, kid), 'ios-client')

    assert claims is not None
    assert claims['sub'] == 'google-sub'
    assert claims['email_verified'] is True


@pytest.mark.asyncio
async def test_validate_google_id_token_accepts_matching_nonce(monkeypatch: pytest.MonkeyPatch) -> None:
    private_key_pem, private_key = _rsa_private_key_pem()
    kid = 'GOOGLEKID'
    monkeypatch.setattr(
        oauth_providers,
        '_get_google_jwks',
        AsyncMock(return_value={'keys': [_jwk_for_key(private_key, kid)]}),
    )

    claims = await validate_google_id_token(
        _google_token(private_key_pem, kid, nonce='backend-nonce'), 'ios-client', 'backend-nonce'
    )

    assert claims is not None
    assert claims['sub'] == 'google-sub'


@pytest.mark.asyncio
async def test_validate_google_id_token_rejects_nonce_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    private_key_pem, private_key = _rsa_private_key_pem()
    kid = 'GOOGLEKID'
    monkeypatch.setattr(
        oauth_providers,
        '_get_google_jwks',
        AsyncMock(return_value={'keys': [_jwk_for_key(private_key, kid)]}),
    )

    claims = await validate_google_id_token(
        _google_token(private_key_pem, kid, nonce='other-nonce'), 'ios-client', 'backend-nonce'
    )

    assert claims is None


@pytest.mark.asyncio
async def test_validate_google_id_token_rejects_wrong_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    private_key_pem, private_key = _rsa_private_key_pem()
    kid = 'GOOGLEKID'
    monkeypatch.setattr(
        oauth_providers,
        '_get_google_jwks',
        AsyncMock(return_value={'keys': [_jwk_for_key(private_key, kid)]}),
    )

    claims = await validate_google_id_token(_google_token(private_key_pem, kid, audience='other-client'), 'ios-client')

    assert claims is None


@pytest.mark.asyncio
async def test_validate_google_id_token_rejects_wrong_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    private_key_pem, private_key = _rsa_private_key_pem()
    kid = 'GOOGLEKID'
    monkeypatch.setattr(
        oauth_providers,
        '_get_google_jwks',
        AsyncMock(return_value={'keys': [_jwk_for_key(private_key, kid)]}),
    )

    claims = await validate_google_id_token(
        _google_token(private_key_pem, kid, issuer='https://evil.example'), 'ios-client'
    )

    assert claims is None


@pytest.mark.asyncio
async def test_validate_google_id_token_rejects_expired_token(monkeypatch: pytest.MonkeyPatch) -> None:
    private_key_pem, private_key = _rsa_private_key_pem()
    kid = 'GOOGLEKID'
    monkeypatch.setattr(
        oauth_providers,
        '_get_google_jwks',
        AsyncMock(return_value={'keys': [_jwk_for_key(private_key, kid)]}),
    )

    claims = await validate_google_id_token(
        _google_token(private_key_pem, kid, expires_delta=timedelta(minutes=-5)),
        'ios-client',
    )

    assert claims is None


@pytest.mark.asyncio
async def test_validate_google_id_token_rejects_unknown_kid(monkeypatch: pytest.MonkeyPatch) -> None:
    private_key_pem, _private_key = _rsa_private_key_pem()
    monkeypatch.setattr(oauth_providers, '_get_google_jwks', AsyncMock(return_value={'keys': []}))
    monkeypatch.setattr(oauth_providers, '_force_refresh_google_jwks', AsyncMock(return_value=None))

    claims = await validate_google_id_token(_google_token(private_key_pem, 'UNKNOWNKID'), 'ios-client')

    assert claims is None


@pytest.mark.asyncio
async def test_google_get_user_info_uses_web_audience_for_android_credential_manager_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    validate = AsyncMock(
        return_value={
            'sub': 'google-sub',
            'email': 'alice@example.com',
            'email_verified': 'true',
            'given_name': 'Alice',
            'family_name': 'Google',
            'picture': 'https://example.com/alice.png',
        }
    )
    monkeypatch.setattr(oauth_providers, 'validate_google_id_token', validate)

    info = await provider.get_user_info(
        {'id_token': 'id-token', '_google_client_type': 'android', '_google_nonce': 'backend-nonce'}
    )

    validate.assert_awaited_once_with('id-token', 'web-client', 'backend-nonce')
    assert info.provider == 'google'
    assert info.provider_id == 'google-sub'
    assert info.email == 'alice@example.com'
    assert info.email_verified is True
    assert info.first_name == 'Alice'
    assert info.last_name == 'Google'
    assert info.avatar_url == 'https://example.com/alice.png'


@pytest.mark.asyncio
async def test_google_web_exchange_code_still_uses_authorization_code(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider()
    monkeypatch.setattr(oauth_providers.httpx, 'AsyncClient', _FakeAsyncClient)

    token_data = await provider.exchange_code('auth-code')

    assert token_data['access_token'] == 'access-token'
    assert _FakeAsyncClient.last_post_json is not None
    assert _FakeAsyncClient.last_post_json['client_id'] == 'web-client'
    assert _FakeAsyncClient.last_post_json['client_secret'] == 'web-secret'
    assert _FakeAsyncClient.last_post_json['code'] == 'auth-code'
    assert _FakeAsyncClient.last_post_json['redirect_uri'] == 'https://cabinet.example.com/auth/oauth/callback'
