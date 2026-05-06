"""PKCE and nonce generator tests."""

import base64
import hashlib
import re


def test_generate_pkce_pair_verifier_length():
    from app.cabinet.auth.telegram_auth import generate_pkce_pair
    verifier, _ = generate_pkce_pair()
    assert 43 <= len(verifier) <= 128


def test_generate_pkce_pair_verifier_url_safe():
    from app.cabinet.auth.telegram_auth import generate_pkce_pair
    verifier, _ = generate_pkce_pair()
    assert re.fullmatch(r'[A-Za-z0-9\-._~]+', verifier)


def test_generate_pkce_pair_challenge_matches_verifier():
    from app.cabinet.auth.telegram_auth import generate_pkce_pair
    verifier, challenge = generate_pkce_pair()
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    expected = base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')
    assert challenge == expected


def test_generate_pkce_pair_unique():
    from app.cabinet.auth.telegram_auth import generate_pkce_pair
    pairs = {generate_pkce_pair()[0] for _ in range(20)}
    assert len(pairs) == 20


def test_generate_oidc_nonce_length_and_alphabet():
    from app.cabinet.auth.telegram_auth import generate_oidc_nonce
    nonce = generate_oidc_nonce()
    assert len(nonce) == 32
    assert re.fullmatch(r'[0-9a-f]{32}', nonce)


def test_generate_oidc_nonce_unique():
    from app.cabinet.auth.telegram_auth import generate_oidc_nonce
    nonces = {generate_oidc_nonce() for _ in range(20)}
    assert len(nonces) == 20
