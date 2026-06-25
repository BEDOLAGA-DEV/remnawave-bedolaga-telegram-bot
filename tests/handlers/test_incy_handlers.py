from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
import app.handlers.subscription.incy as incy


def _callback(data):
    msg = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock(), delete=AsyncMock())
    return SimpleNamespace(data=data, message=msg, answer=AsyncMock())


def _user():
    return SimpleNamespace(id=1, language='ru')


@pytest.mark.asyncio
async def test_incy_connect_uses_plain_override_url_not_crypt5(monkeypatch):
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', 'cdn.example.com', raising=False)
    monkeypatch.setattr(settings, 'INCY_CONNECT_REDIRECT_TEMPLATE', 'https://r.example/?redirect_to=', raising=False)

    sub = SimpleNamespace(
        id=5,
        subscription_url='https://old.host/tok',
        subscription_crypto_link='happ://crypt5SHOULD_NOT_BE_USED',
    )

    async def fake_resolve(callback, db_user, db, state=None):
        return sub, 5

    monkeypatch.setattr(incy, 'resolve_subscription_from_context', fake_resolve)

    captured = {}

    def fake_encrypt(url, name=None):
        captured['url'] = url
        captured['name'] = name
        return 'incy://crypt1/PAYLOAD'

    monkeypatch.setattr(incy, 'encrypt_incy_link', fake_encrypt)

    cb = _callback('nz!_capp:incy:5')
    await incy.handle_connect_incy(cb, _user(), db=None, state=None)

    # Must encrypt the override-applied PLAIN url, never the crypt5 link
    assert captured['url'] == 'https://cdn.example.com/tok'
    cb.message.answer.assert_awaited()  # message shown


@pytest.mark.asyncio
async def test_incy_download_windows_resolves_release(monkeypatch):
    async def fake_assets(force=False):
        return {'windows': 'https://gh/incy-windows-setup.exe'}

    monkeypatch.setattr(incy, 'get_incy_desktop_assets', fake_assets)

    cb = _callback('nz!_incy_dl:windows')
    await incy.handle_incy_download(cb, _user(), db=None, state=None)
    cb.message.edit_text.assert_awaited()


@pytest.mark.asyncio
async def test_incy_download_android_uses_store_url(monkeypatch):
    monkeypatch.setattr(settings, 'INCY_ANDROID_URL', 'https://play.google.com/x', raising=False)
    cb = _callback('nz!_incy_dl:android')
    await incy.handle_incy_download(cb, _user(), db=None, state=None)
    cb.message.edit_text.assert_awaited()


@pytest.mark.asyncio
async def test_incy_download_macos_menu(monkeypatch):
    cb = _callback('nz!_incy_dl:macos')
    await incy.handle_incy_download(cb, _user(), db=None, state=None)
    cb.message.edit_text.assert_awaited()


@pytest.mark.asyncio
async def test_incy_download_missing_asset_alerts(monkeypatch):
    async def fake_assets(force=False):
        return {}  # nothing resolved

    monkeypatch.setattr(incy, 'get_incy_desktop_assets', fake_assets)
    cb = _callback('nz!_incy_dl:windows')
    await incy.handle_incy_download(cb, _user(), db=None, state=None)
    cb.answer.assert_awaited()  # show_alert path
