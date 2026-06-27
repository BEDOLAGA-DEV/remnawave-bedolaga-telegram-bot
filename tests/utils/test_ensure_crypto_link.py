import pytest

from app.config import settings
from app.utils.subscription_utils import (
    ensure_subscription_crypto_link,
    get_cryptolink_connect_link,
)


class FakeApi:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def encrypt_happ_crypto_link(self, link):
        self.calls.append(link)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeService:
    api = None

    def get_api_client(self):
        return FakeService.api


class FakeDb:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _sub(url, crypto):
    from types import SimpleNamespace

    return SimpleNamespace(subscription_url=url, subscription_crypto_link=crypto)


def _patch_service(monkeypatch, api):
    FakeService.api = api
    import app.services.remnawave_service as rs

    monkeypatch.setattr(rs, 'RemnaWaveService', FakeService, raising=False)


def _cryptolink(monkeypatch):
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'happ_cryptolink', raising=False)


@pytest.mark.asyncio
async def test_existing_crypto_returned_without_api(monkeypatch):
    _cryptolink(monkeypatch)
    api = FakeApi('happ://crypt5NEW')
    _patch_service(monkeypatch, api)
    db = FakeDb()
    sub = _sub('https://old.host/t', 'happ://crypt5OLD')
    out = await ensure_subscription_crypto_link(db, sub)
    assert out == 'happ://crypt5OLD'
    assert api.calls == []
    assert db.commits == 0


@pytest.mark.asyncio
async def test_generates_and_persists_when_missing(monkeypatch):
    _cryptolink(monkeypatch)
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', 'cdn.example.com', raising=False)
    api = FakeApi('happ://crypt5GEN')
    _patch_service(monkeypatch, api)
    db = FakeDb()
    sub = _sub('https://old.host/t', None)
    out = await ensure_subscription_crypto_link(db, sub)
    assert out == 'happ://crypt5GEN'
    assert sub.subscription_crypto_link == 'happ://crypt5GEN'
    assert db.commits == 1
    # encrypted the override-applied url
    assert api.calls == ['https://cdn.example.com/t']


@pytest.mark.asyncio
async def test_returns_none_when_generation_fails(monkeypatch):
    _cryptolink(monkeypatch)
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', '', raising=False)
    api = FakeApi(None)
    _patch_service(monkeypatch, api)
    db = FakeDb()
    sub = _sub('https://old.host/t', None)
    out = await ensure_subscription_crypto_link(db, sub)
    assert out is None
    assert sub.subscription_crypto_link is None


@pytest.mark.asyncio
async def test_swallows_exception(monkeypatch):
    _cryptolink(monkeypatch)
    api = FakeApi(RuntimeError('boom'))
    _patch_service(monkeypatch, api)
    db = FakeDb()
    sub = _sub('https://old.host/t', None)
    out = await ensure_subscription_crypto_link(db, sub)
    assert out is None


@pytest.mark.asyncio
async def test_noop_outside_cryptolink_mode(monkeypatch):
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'link', raising=False)
    api = FakeApi('happ://crypt5GEN')
    _patch_service(monkeypatch, api)
    db = FakeDb()
    sub = _sub('https://old.host/t', None)
    out = await ensure_subscription_crypto_link(db, sub)
    assert out is None
    assert api.calls == []


@pytest.mark.asyncio
async def test_none_subscription(monkeypatch):
    _cryptolink(monkeypatch)
    db = FakeDb()
    assert await ensure_subscription_crypto_link(db, None) is None


@pytest.mark.asyncio
async def test_connect_link_prefers_crypto(monkeypatch):
    _cryptolink(monkeypatch)
    api = FakeApi('happ://crypt5GEN')
    _patch_service(monkeypatch, api)
    db = FakeDb()
    sub = _sub('https://old.host/t', 'happ://crypt5STORED')
    out = await get_cryptolink_connect_link(db, sub)
    assert out == 'happ://crypt5STORED'


@pytest.mark.asyncio
async def test_connect_link_falls_back_to_happ_scheme(monkeypatch):
    _cryptolink(monkeypatch)
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', 'cdn.example.com', raising=False)
    api = FakeApi(None)  # generation fails
    _patch_service(monkeypatch, api)
    db = FakeDb()
    sub = _sub('https://old.host/tok', None)
    out = await get_cryptolink_connect_link(db, sub)
    # never plain https — happ scheme of the overridden host
    assert out == 'happ://cdn.example.com/tok'
    assert not out.startswith('https://')


@pytest.mark.asyncio
async def test_connect_link_none_when_no_url(monkeypatch):
    _cryptolink(monkeypatch)
    api = FakeApi(None)
    _patch_service(monkeypatch, api)
    db = FakeDb()
    sub = _sub(None, None)
    out = await get_cryptolink_connect_link(db, sub)
    assert out is None


@pytest.mark.asyncio
async def test_connect_link_guards_schemeless_url(monkeypatch):
    _cryptolink(monkeypatch)
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', '', raising=False)
    api = FakeApi(None)
    _patch_service(monkeypatch, api)
    db = FakeDb()
    sub = _sub('justtoken', None)  # no scheme -> convert returns it unchanged -> must be rejected
    out = await get_cryptolink_connect_link(db, sub)
    assert out is None
