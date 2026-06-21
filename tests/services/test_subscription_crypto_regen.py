import pytest

import app.services.subscription_service as svc
from app.config import settings


class FakeApi:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def encrypt_happ_crypto_link(self, link):
        return f'happ://crypt5::{link}'


async def _noop_sleep(*_a, **_k):
    return None


@pytest.mark.asyncio
async def test_regen_updates_active_subs(monkeypatch):
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', 'cdn.example.com', raising=False)

    class Sub:
        def __init__(self):
            self.id = 1
            self.subscription_url = 'https://old.host/tok'
            self.subscription_crypto_link = 'happ://crypt5OLD'

    subs = [Sub()]

    async def fake_loader():
        return subs

    class FakeDb:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, model, pk):
            return subs[0] if pk == 1 else None

        async def commit(self):
            return None

    monkeypatch.setattr(svc, '_load_subs_for_crypto_regen', fake_loader, raising=False)
    monkeypatch.setattr(svc, '_open_panel_api_for_regen', lambda: FakeApi(), raising=False)
    monkeypatch.setattr(svc, 'AsyncSessionLocal', lambda: FakeDb(), raising=False)
    monkeypatch.setattr(svc.asyncio, 'sleep', _noop_sleep, raising=False)

    count = await svc.regenerate_all_subscription_crypto_links()

    assert count == 1
    assert subs[0].subscription_crypto_link == 'happ://crypt5::https://cdn.example.com/tok'


@pytest.mark.asyncio
async def test_regen_noop_when_override_unset(monkeypatch):
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', '', raising=False)
    count = await svc.regenerate_all_subscription_crypto_links()
    assert count == 0
