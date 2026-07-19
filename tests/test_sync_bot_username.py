"""Regression test for Telegram bug #650370.

A migrated bot (new token) kept generating gift-claim links pointing at the OLD bot because
those links read settings.BOT_USERNAME (a manually-maintained env) while the username is
actually derived from the token by Telegram. sync_bot_username() now overrides
settings.BOT_USERNAME from the live get_me() at startup, so every get_bot_username() caller
(gift links included) follows the real bot and a token swap self-heals.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.utils.bot_identity import sync_bot_username


@pytest.mark.asyncio
async def test_sync_overrides_stale_username(monkeypatch):
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'old_bot')
    fake_bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username='new_bot')))

    await sync_bot_username(fake_bot)

    assert settings.BOT_USERNAME == 'new_bot'


@pytest.mark.asyncio
async def test_sync_keeps_config_on_get_me_failure(monkeypatch):
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'old_bot')
    fake_bot = SimpleNamespace(get_me=AsyncMock(side_effect=RuntimeError('network down')))

    await sync_bot_username(fake_bot)

    assert settings.BOT_USERNAME == 'old_bot'


@pytest.mark.asyncio
async def test_sync_noop_when_already_correct(monkeypatch):
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'same_bot')
    fake_bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username='same_bot')))

    await sync_bot_username(fake_bot)

    assert settings.BOT_USERNAME == 'same_bot'
