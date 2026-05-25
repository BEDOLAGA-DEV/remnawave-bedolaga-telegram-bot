from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Self
from unittest.mock import AsyncMock

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.cabinet.auth import oauth_providers
from app.cabinet.auth.oauth_providers import APPLE_ISSUER, AppleProvider, validate_apple_id_token
from app.database.crud.user import OAUTH_PROVIDER_COLUMNS
from app.services.rbac_bootstrap_service import TRUSTED_EMAIL_VERIFICATION_SOURCES


class _FakeTokenResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, str]:
        return {'id_token': 'server-id-token'}


class _FakeAsyncClient:
    last_post_data: dict[str, str] | None = None

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, _url: str, *, data: dict[str, str], headers: dict[str, str]) -> _FakeTokenResponse:
        self.__class__.last_post_data = data
        return _FakeTokenResponse()


def _ec_private_key_pem() -> tuple[str, ec.EllipticCurvePrivateKey]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return pem, private_key


def _jwk_for_key(private_key: ec.EllipticCurvePrivateKey, kid: str) -> dict[str, str]:
    jwk = json.loads(pyjwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key()))
    jwk['kid'] = kid
    jwk['alg'] = 'ES256'
    return jwk


def test_apple_client_secret_is_es256_jwt() -> None:
    private_key_pem, private_key = _ec_private_key_pem()
    provider = AppleProvider(
        client_id='com.example.service',
        client_secret='',
        redirect_uri='https://cabinet.example.com/auth/oauth/callback',
        web_client_id='com.example.service',
        ios_client_id='com.example.app',
        team_id='TEAM123456',
        key_id='KEY1234567',
        private_key=private_key_pem,
    )

    now = datetime(2026, 5, 16, tzinfo=UTC)
    token = provider.create_client_secret(now=now)

    header = pyjwt.get_unverified_header(token)
    assert header['alg'] == 'ES256'
    assert header['kid'] == 'KEY1234567'

    claims = pyjwt.decode(
        token,
        key=private_key.public_key(),
        algorithms=['ES256'],
        audience=APPLE_ISSUER,
    )
    assert claims['iss'] == 'TEAM123456'
    assert claims['sub'] == 'com.example.service'
    assert claims['exp'] - claims['iat'] == int(timedelta(days=180).total_seconds())


@pytest.mark.asyncio
async def test_apple_exchange_can_omit_redirect_uri_for_native_ios(monkeypatch: pytest.MonkeyPatch) -> None:
    private_key_pem, _private_key = _ec_private_key_pem()
    provider = AppleProvider(
        client_id='com.example.service',
        client_secret='',
        redirect_uri='https://cabinet.example.com/auth/oauth/callback',
        web_client_id='com.example.service',
        ios_client_id='com.example.app',
        team_id='TEAM123456',
        key_id='KEY1234567',
        private_key=private_key_pem,
    )
    monkeypatch.setattr(oauth_providers.httpx, 'AsyncClient', _FakeAsyncClient)
    monkeypatch.setattr(provider, 'create_client_secret', lambda client_type='web': f'{client_type}-client-secret')

    await provider.exchange_code('auth-code', client_type='ios')

    assert _FakeAsyncClient.last_post_data is not None
    assert _FakeAsyncClient.last_post_data['client_id'] == 'com.example.app'
    assert _FakeAsyncClient.last_post_data['client_secret'] == 'ios-client-secret'
    assert 'redirect_uri' not in _FakeAsyncClient.last_post_data


@pytest.mark.asyncio
async def test_apple_exchange_includes_redirect_uri_for_web(monkeypatch: pytest.MonkeyPatch) -> None:
    private_key_pem, _private_key = _ec_private_key_pem()
    provider = AppleProvider(
        client_id='com.example.service',
        client_secret='',
        redirect_uri='https://cabinet.example.com/auth/oauth/callback',
        web_client_id='com.example.service',
        ios_client_id='com.example.app',
        team_id='TEAM123456',
        key_id='KEY1234567',
        private_key=private_key_pem,
    )
    monkeypatch.setattr(oauth_providers.httpx, 'AsyncClient', _FakeAsyncClient)
    monkeypatch.setattr(provider, 'create_client_secret', lambda client_type='web': f'{client_type}-client-secret')

    await provider.exchange_code('auth-code', client_type='web')

    assert _FakeAsyncClient.last_post_data is not None
    assert _FakeAsyncClient.last_post_data['client_id'] == 'com.example.service'
    assert _FakeAsyncClient.last_post_data['client_secret'] == 'web-client-secret'
    assert _FakeAsyncClient.last_post_data['redirect_uri'] == 'https://cabinet.example.com/auth/oauth/callback'


@pytest.mark.asyncio
async def test_apple_exchange_requires_server_id_token(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NoIdTokenResponse(_FakeTokenResponse):
        def json(self) -> dict[str, str]:
            return {}

    class _NoIdTokenAsyncClient(_FakeAsyncClient):
        async def post(self, _url: str, *, data: dict[str, str], headers: dict[str, str]) -> _NoIdTokenResponse:
            self.__class__.last_post_data = data
            return _NoIdTokenResponse()

    private_key_pem, _private_key = _ec_private_key_pem()
    provider = AppleProvider(
        client_id='com.example.service',
        client_secret='',
        redirect_uri='https://cabinet.example.com/auth/oauth/callback',
        web_client_id='com.example.service',
        ios_client_id='com.example.app',
        team_id='TEAM123456',
        key_id='KEY1234567',
        private_key=private_key_pem,
    )
    monkeypatch.setattr(oauth_providers.httpx, 'AsyncClient', _NoIdTokenAsyncClient)
    monkeypatch.setattr(provider, 'create_client_secret', lambda client_type='web': f'{client_type}-client-secret')

    with pytest.raises(ValueError, match='missing id_token'):
        await provider.exchange_code('auth-code', client_type='web', id_token='client-id-token')


@pytest.mark.asyncio
async def test_validate_apple_id_token_accepts_ios_hashed_nonce(monkeypatch: pytest.MonkeyPatch) -> None:
    private_key_pem, private_key = _ec_private_key_pem()
    kid = 'APPLEKID'
    monkeypatch.setattr(
        oauth_providers,
        '_get_apple_jwks',
        AsyncMock(return_value={'keys': [_jwk_for_key(private_key, kid)]}),
    )

    raw_nonce = 'backend-nonce'
    token = pyjwt.encode(
        {
            'iss': APPLE_ISSUER,
            'aud': 'com.example.service',
            'sub': 'apple-sub',
            'iat': int(datetime.now(UTC).timestamp()),
            'exp': int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            'nonce': oauth_providers._sha256_urlsafe(raw_nonce),
            'email': 'alice@example.com',
            'email_verified': 'true',
        },
        private_key_pem,
        algorithm='ES256',
        headers={'kid': kid},
    )

    claims = await validate_apple_id_token(token, 'com.example.service', nonce=raw_nonce)

    assert claims is not None
    assert claims['sub'] == 'apple-sub'
    assert claims['email_verified'] == 'true'


@pytest.mark.asyncio
async def test_validate_apple_id_token_rejects_nonce_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    private_key_pem, private_key = _ec_private_key_pem()
    kid = 'APPLEKID'
    monkeypatch.setattr(
        oauth_providers,
        '_get_apple_jwks',
        AsyncMock(return_value={'keys': [_jwk_for_key(private_key, kid)]}),
    )

    token = pyjwt.encode(
        {
            'iss': APPLE_ISSUER,
            'aud': 'com.example.service',
            'sub': 'apple-sub',
            'iat': int(datetime.now(UTC).timestamp()),
            'exp': int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            'nonce': 'different-nonce',
        },
        private_key_pem,
        algorithm='ES256',
        headers={'kid': kid},
    )

    assert await validate_apple_id_token(token, 'com.example.service', nonce='backend-nonce') is None


@pytest.mark.asyncio
async def test_apple_get_user_info_uses_signed_claims_and_first_login_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AppleProvider(
        client_id='com.example.service',
        client_secret='',
        redirect_uri='https://cabinet.example.com/auth/oauth/callback',
        web_client_id='com.example.service',
        ios_client_id='com.example.app',
        team_id='TEAM123456',
        key_id='KEY1234567',
        private_key='unused-in-this-test',
    )
    monkeypatch.setattr(
        oauth_providers,
        'validate_apple_id_token',
        AsyncMock(
            return_value={
                'sub': 'apple-sub',
                'email': 'alice@example.com',
                'email_verified': 'true',
            }
        ),
    )

    info = await provider.get_user_info(
        {
            'id_token': 'id-token',
            '_apple_nonce': 'nonce',
            '_apple_user': '{"name":{"firstName":"Alice","lastName":"Appleseed"},"email":"first@example.com"}',
        }
    )

    assert info.provider == 'apple'
    assert info.provider_id == 'apple-sub'
    assert info.email == 'alice@example.com'
    assert info.email_verified is True
    assert info.first_name == 'Alice'
    assert info.last_name == 'Appleseed'


@pytest.mark.asyncio
async def test_apple_get_user_info_ignores_client_supplied_email_without_signed_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AppleProvider(
        client_id='com.example.service',
        client_secret='',
        redirect_uri='https://cabinet.example.com/auth/oauth/callback',
        web_client_id='com.example.service',
        ios_client_id='com.example.app',
        team_id='TEAM123456',
        key_id='KEY1234567',
        private_key='unused-in-this-test',
    )
    monkeypatch.setattr(
        oauth_providers,
        'validate_apple_id_token',
        AsyncMock(return_value={'sub': 'apple-sub', 'email_verified': 'true'}),
    )

    info = await provider.get_user_info(
        {
            'id_token': 'id-token',
            '_apple_user': '{"name":{"firstName":"Alice"},"email":"client@example.com"}',
        }
    )

    assert info.provider_id == 'apple-sub'
    assert info.email is None
    assert info.email_verified is False
    assert info.first_name == 'Alice'


@pytest.mark.asyncio
async def test_apple_get_user_info_validates_ios_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = AppleProvider(
        client_id='com.example.service',
        client_secret='',
        redirect_uri='https://cabinet.example.com/auth/oauth/callback',
        web_client_id='com.example.service',
        ios_client_id='com.example.app',
        team_id='TEAM123456',
        key_id='KEY1234567',
        private_key='unused-in-this-test',
    )
    validate = AsyncMock(return_value={'sub': 'apple-sub'})
    monkeypatch.setattr(oauth_providers, 'validate_apple_id_token', validate)

    info = await provider.get_user_info({'id_token': 'id-token', '_apple_client_type': 'ios'})

    validate.assert_awaited_once_with('id-token', 'com.example.app', None)
    assert info.provider_id == 'apple-sub'


def test_apple_provider_storage_and_trust_wiring() -> None:
    assert OAUTH_PROVIDER_COLUMNS['apple'] == 'apple_id'
    assert 'oauth_apple' in TRUSTED_EMAIL_VERIFICATION_SOURCES
