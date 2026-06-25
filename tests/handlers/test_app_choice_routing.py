from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
import app.handlers.subscription.links as links


def _callback(data):
    msg = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())
    return SimpleNamespace(data=data, message=msg, answer=AsyncMock())


def _user():
    return SimpleNamespace(id=1, language='ru')


@pytest.mark.asyncio
async def test_connect_shows_app_choice(monkeypatch):
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'happ_cryptolink', raising=False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False, raising=False)

    sub = SimpleNamespace(id=5, subscription_url='https://h/tok', subscription_crypto_link='happ://crypt5x')

    async def fake_resolve(callback, db_user, db, state=None):
        return sub, 5

    monkeypatch.setattr(links, 'resolve_subscription_from_context', fake_resolve)
    monkeypatch.setattr(links, 'get_display_subscription_link', lambda s: 'happ://crypt5x')

    cb = _callback('nz!_subscription_connect')
    await links.handle_connect_subscription(cb, _user(), db=None, state=None)

    cb.message.edit_text.assert_awaited()
    _, kwargs = cb.message.edit_text.call_args
    markup = kwargs['reply_markup']
    cbs = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
    assert any(c.startswith('nz!_capp:happ') for c in cbs)
    assert any(c.startswith('nz!_capp:incy') for c in cbs)


@pytest.mark.asyncio
async def test_app_happ_renders_existing_connect_ui(monkeypatch):
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'happ_cryptolink', raising=False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False, raising=False)

    sub = SimpleNamespace(id=5, subscription_url='https://h/tok', subscription_crypto_link='happ://crypt5x')

    async def fake_resolve(callback, db_user, db, state=None):
        return sub, 5

    monkeypatch.setattr(links, 'resolve_subscription_from_context', fake_resolve)
    monkeypatch.setattr(links, 'get_display_subscription_link', lambda s: 'happ://crypt5x')

    cb = _callback('nz!_capp:happ:5')
    await links.handle_connect_app_happ(cb, _user(), db=None, state=None)

    cb.message.edit_text.assert_awaited()
