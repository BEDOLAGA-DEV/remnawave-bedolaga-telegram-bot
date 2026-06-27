from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
import app.handlers.subscription.common as common
import app.handlers.subscription.links as links


def _callback(data):
    msg = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())
    return SimpleNamespace(data=data, message=msg, answer=AsyncMock())


def _user():
    return SimpleNamespace(id=1, language='ru')


def _setup(monkeypatch, connect_value):
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'happ_cryptolink', raising=False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False, raising=False)
    sub = SimpleNamespace(id=5, subscription_url='https://old.host/tok', subscription_crypto_link=None)

    async def fake_resolve(callback, db_user, db, state=None):
        return sub, 5

    monkeypatch.setattr(common, 'resolve_subscription_from_context', fake_resolve)
    # gate value (line 303) is truthy so the handler reaches the cryptolink branch
    monkeypatch.setattr(links, 'get_display_subscription_link', lambda s: 'https://old.host/tok')

    async def fake_connect(db, subscription):
        return connect_value

    monkeypatch.setattr(links, 'get_cryptolink_connect_link', fake_connect)
    return sub


def _sent_text(cb):
    args, kwargs = cb.message.answer.call_args
    return args[0] if args else kwargs.get('text', '')


@pytest.mark.asyncio
async def test_cryptolink_shows_crypt5_never_plain(monkeypatch):
    _setup(monkeypatch, 'happ://crypt5GEN')
    cb = _callback('nz!_open_subscription_link')
    await links.handle_open_subscription_link(cb, _user(), db=None, state=None)

    cb.message.answer.assert_awaited()
    text = _sent_text(cb)
    assert 'happ://crypt5GEN' in text
    assert 'https://' not in text  # the plain URL must never appear


@pytest.mark.asyncio
async def test_cryptolink_falls_back_to_happ_scheme_never_plain(monkeypatch):
    _setup(monkeypatch, 'happ://cdn.example.com/tok')
    cb = _callback('nz!_open_subscription_link')
    await links.handle_open_subscription_link(cb, _user(), db=None, state=None)

    cb.message.answer.assert_awaited()
    text = _sent_text(cb)
    assert 'happ://cdn.example.com/tok' in text
    assert 'https://' not in text


@pytest.mark.asyncio
async def test_cryptolink_no_link_shows_alert(monkeypatch):
    _setup(monkeypatch, None)
    cb = _callback('nz!_open_subscription_link')
    await links.handle_open_subscription_link(cb, _user(), db=None, state=None)

    cb.message.answer.assert_not_awaited()  # no message with a plain link
    cb.answer.assert_awaited()
