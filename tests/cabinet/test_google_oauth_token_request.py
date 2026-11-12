"""Параметры обмена Google OAuth code должны передаваться как form data."""

from urllib.parse import parse_qs

import httpx
import pytest

from app.cabinet.auth import oauth_providers


@pytest.mark.asyncio
async def test_google_exchange_code_sends_form_encoded_body(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={'access_token': 'test-access-token'})

    original_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        return original_client(*args, transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(oauth_providers.httpx, 'AsyncClient', mock_client)

    provider = oauth_providers.GoogleProvider(
        client_id='test-client-id',
        client_secret='test-secret+/=',
        redirect_uri='https://cabinet.example.com/auth/oauth/callback',
    )
    result = await provider.exchange_code('test-code+/=')

    assert result['access_token'] == 'test-access-token'
    assert len(requests) == 1
    request = requests[0]
    assert request.method == 'POST'
    assert str(request.url) == 'https://oauth2.googleapis.com/token'
    assert request.headers['Content-Type'].startswith('application/x-www-form-urlencoded')
    assert parse_qs(request.content.decode('utf-8')) == {
        'client_id': ['test-client-id'],
        'client_secret': ['test-secret+/='],
        'code': ['test-code+/='],
        'grant_type': ['authorization_code'],
        'redirect_uri': ['https://cabinet.example.com/auth/oauth/callback'],
    }
