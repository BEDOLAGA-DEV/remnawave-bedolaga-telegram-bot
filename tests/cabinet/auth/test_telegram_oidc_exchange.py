"""Tests for exchange_authorization_code."""

import httpx
import pytest


@pytest.mark.asyncio
async def test_exchange_success(monkeypatch):
    from app.cabinet.auth import telegram_auth

    captured: dict = {}

    class _MockResponse:
        status_code = 200

        def json(self):
            return {
                'id_token': 'fake.jwt.token',
                'access_token': 'access',
                'token_type': 'bearer',
                'expires_in': 3600,
            }

        def raise_for_status(self):
            return None

    class _MockClient:
        def __init__(self, *args, **kwargs):
            captured['client_kwargs'] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, data, headers, auth):
            captured['url'] = url
            captured['data'] = data
            captured['auth'] = auth
            return _MockResponse()

    monkeypatch.setattr(telegram_auth.httpx, 'AsyncClient', _MockClient)

    token = await telegram_auth.exchange_authorization_code(
        code='auth_code_xyz',
        code_verifier='verifier_abc',
        redirect_uri='https://cab.example.com/cb',
        client_id='111',
        client_secret='secret',
    )

    assert token == 'fake.jwt.token'
    assert captured['url'] == 'https://oauth.telegram.org/token'
    assert captured['data']['grant_type'] == 'authorization_code'
    assert captured['data']['code'] == 'auth_code_xyz'
    assert captured['data']['code_verifier'] == 'verifier_abc'
    assert captured['data']['redirect_uri'] == 'https://cab.example.com/cb'
    assert captured['data']['client_id'] == '111'
    assert captured['auth'] == ('111', 'secret')


@pytest.mark.asyncio
async def test_exchange_4xx_returns_none(monkeypatch):
    from app.cabinet.auth import telegram_auth

    class _MockResponse:
        status_code = 400

        def json(self):
            return {'error': 'invalid_grant'}

        def raise_for_status(self):
            raise httpx.HTTPStatusError('400', request=httpx.Request('POST', 'https://x'), response=httpx.Response(400))

    class _MockClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kw):
            return _MockResponse()

    monkeypatch.setattr(telegram_auth.httpx, 'AsyncClient', _MockClient)

    result = await telegram_auth.exchange_authorization_code(
        code='c',
        code_verifier='v',
        redirect_uri='https://cab.example.com/cb',
        client_id='111',
        client_secret='secret',
    )
    assert result is None


@pytest.mark.asyncio
async def test_exchange_timeout_returns_none(monkeypatch):
    from app.cabinet.auth import telegram_auth

    class _MockClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            raise httpx.TimeoutException('slow')

    monkeypatch.setattr(telegram_auth.httpx, 'AsyncClient', _MockClient)

    result = await telegram_auth.exchange_authorization_code(
        code='c',
        code_verifier='v',
        redirect_uri='https://cab.example.com/cb',
        client_id='111',
        client_secret='secret',
    )
    assert result is None


@pytest.mark.asyncio
async def test_exchange_no_id_token_in_response(monkeypatch):
    from app.cabinet.auth import telegram_auth

    class _MockResponse:
        status_code = 200

        def json(self):
            return {'access_token': 'access'}  # missing id_token

        def raise_for_status(self):
            return None

    class _MockClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            return _MockResponse()

    monkeypatch.setattr(telegram_auth.httpx, 'AsyncClient', _MockClient)

    result = await telegram_auth.exchange_authorization_code(
        code='c',
        code_verifier='v',
        redirect_uri='https://cab.example.com/cb',
        client_id='111',
        client_secret='secret',
    )
    assert result is None
