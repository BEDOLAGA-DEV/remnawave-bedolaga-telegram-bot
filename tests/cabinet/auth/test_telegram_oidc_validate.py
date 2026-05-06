"""Tests for validate_telegram_oidc_token nonce handling."""

import pytest


@pytest.mark.asyncio
async def test_validate_oidc_token_no_nonce_required(make_id_token, jwks_doc, monkeypatch):
    """If expected_nonce is None, claims.nonce is ignored even when present."""
    from app.cabinet.auth import telegram_auth

    async def _fake_get_jwks(force: bool = False):
        return jwks_doc

    monkeypatch.setattr(telegram_auth, '_get_jwks', _fake_get_jwks)

    token = make_id_token(client_id='111', nonce='abc123')
    claims = await telegram_auth.validate_telegram_oidc_token(token, '111')
    assert claims is not None
    assert claims['sub'] == '1234567890'


@pytest.mark.asyncio
async def test_validate_oidc_token_nonce_match(make_id_token, jwks_doc, monkeypatch):
    from app.cabinet.auth import telegram_auth

    async def _fake_get_jwks(force: bool = False):
        return jwks_doc

    monkeypatch.setattr(telegram_auth, '_get_jwks', _fake_get_jwks)

    token = make_id_token(client_id='111', nonce='nonce-xyz')
    claims = await telegram_auth.validate_telegram_oidc_token(token, '111', expected_nonce='nonce-xyz')
    assert claims is not None
    assert claims['nonce'] == 'nonce-xyz'


@pytest.mark.asyncio
async def test_validate_oidc_token_nonce_mismatch(make_id_token, jwks_doc, monkeypatch):
    from app.cabinet.auth import telegram_auth

    async def _fake_get_jwks(force: bool = False):
        return jwks_doc

    monkeypatch.setattr(telegram_auth, '_get_jwks', _fake_get_jwks)

    token = make_id_token(client_id='111', nonce='nonce-xyz')
    claims = await telegram_auth.validate_telegram_oidc_token(token, '111', expected_nonce='nonce-other')
    assert claims is None


@pytest.mark.asyncio
async def test_validate_oidc_token_missing_nonce_when_expected(make_id_token, jwks_doc, monkeypatch):
    """If expected_nonce is set but token has no nonce claim, validation fails."""
    from app.cabinet.auth import telegram_auth

    async def _fake_get_jwks(force: bool = False):
        return jwks_doc

    monkeypatch.setattr(telegram_auth, '_get_jwks', _fake_get_jwks)

    token = make_id_token(client_id='111')  # no nonce
    claims = await telegram_auth.validate_telegram_oidc_token(token, '111', expected_nonce='something')
    assert claims is None
