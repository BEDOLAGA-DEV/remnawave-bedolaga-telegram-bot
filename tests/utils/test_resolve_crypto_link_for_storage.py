import pytest

from app.config import settings
from app.utils.subscription_utils import resolve_crypto_link_for_storage


class FakeApi:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def encrypt_happ_crypto_link(self, link):
        self.calls.append(link)
        return self.result


@pytest.mark.asyncio
async def test_returns_panel_link_when_override_unset(monkeypatch):
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', '', raising=False)
    api = FakeApi('happ://crypt5NEW')
    out = await resolve_crypto_link_for_storage(api, 'https://old.host/t', 'happ://crypt5OLD')
    assert out == 'happ://crypt5OLD'
    assert api.calls == []


@pytest.mark.asyncio
async def test_reencrypts_overridden_url_when_override_set(monkeypatch):
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', 'cdn.example.com', raising=False)
    api = FakeApi('happ://crypt5NEW')
    out = await resolve_crypto_link_for_storage(api, 'https://old.host/t', 'happ://crypt5OLD')
    assert out == 'happ://crypt5NEW'
    assert api.calls == ['https://cdn.example.com/t']


@pytest.mark.asyncio
async def test_falls_back_to_panel_link_when_encrypt_fails(monkeypatch):
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', 'cdn.example.com', raising=False)
    api = FakeApi(None)
    out = await resolve_crypto_link_for_storage(api, 'https://old.host/t', 'happ://crypt5OLD')
    assert out == 'happ://crypt5OLD'


@pytest.mark.asyncio
async def test_returns_panel_link_when_no_subscription_url(monkeypatch):
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', 'cdn.example.com', raising=False)
    api = FakeApi('happ://crypt5NEW')
    out = await resolve_crypto_link_for_storage(api, '', 'happ://crypt5OLD')
    assert out == 'happ://crypt5OLD'
    assert api.calls == []
