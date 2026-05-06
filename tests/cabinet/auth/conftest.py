"""Shared fixtures for cabinet Telegram OIDC tests."""

from __future__ import annotations

import time
from typing import Any

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture(scope='session')
def rsa_key_pair() -> tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    """Generate an RSA key pair for signing test id_tokens."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


@pytest.fixture(scope='session')
def jwks_doc(rsa_key_pair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]) -> dict[str, Any]:
    """JWKS document containing the test public key with kid='test-kid'."""
    _, public = rsa_key_pair
    jwk = pyjwt.algorithms.RSAAlgorithm.to_jwk(public, as_dict=True)
    jwk['kid'] = 'test-kid'
    jwk['use'] = 'sig'
    jwk['alg'] = 'RS256'
    return {'keys': [jwk]}


@pytest.fixture
def make_id_token(rsa_key_pair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]):
    """Factory that signs a JWT id_token with the test private key."""
    private, _ = rsa_key_pair
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    def _make(
        *,
        client_id: str = '111222333',
        sub: str = '1234567890',
        telegram_id: int = 1234567890,
        nonce: str | None = None,
        exp_offset: int = 600,
        extra: dict[str, Any] | None = None,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            'iss': 'https://oauth.telegram.org',
            'aud': client_id,
            'sub': sub,
            'id': telegram_id,
            'iat': now,
            'exp': now + exp_offset,
            'name': 'Test User',
            'preferred_username': 'testuser',
        }
        if nonce is not None:
            claims['nonce'] = nonce
        if extra:
            claims.update(extra)
        return pyjwt.encode(claims, pem, algorithm='RS256', headers={'kid': 'test-kid'})

    return _make
