"""Pre-warm the logo file_id at startup so broadcasts/notifications don't
re-upload the ~700KB logo on the first send of every cycle (file_id caches only
after the first successful send — the amplifier behind the monitoring stall).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import app.utils.message_patch as mp
from app.config import settings
from app.services.startup_notification_service import StartupNotificationService


def _msg(file_id: str, message_id: int = 99) -> MagicMock:
    photo = MagicMock()
    photo.file_id = file_id
    m = MagicMock()
    m.photo = [photo]
    m.message_id = message_id
    return m


def _service(bot, chat_id=-100123, topic_id=None) -> StartupNotificationService:
    svc = StartupNotificationService(bot)
    svc.chat_id = chat_id
    svc.topic_id = topic_id
    return svc


async def test_prewarm_caches_file_id_and_deletes_message(monkeypatch):
    monkeypatch.setattr(mp, '_logo_file_id', None, raising=False)
    monkeypatch.setattr(mp, 'get_logo_media', lambda: object())  # non-str, non-None stub (FSInputFile-like)

    bot = MagicMock()
    bot.send_photo = AsyncMock(return_value=_msg('FID123'))
    bot.delete_message = AsyncMock()

    ok = await _service(bot).prewarm_logo()

    assert ok is True
    assert mp._logo_file_id == 'FID123'
    bot.send_photo.assert_awaited_once()
    bot.delete_message.assert_awaited_once()


async def test_prewarm_skips_when_already_cached(monkeypatch):
    monkeypatch.setattr(mp, '_logo_file_id', 'ALREADY', raising=False)
    bot = MagicMock()
    bot.send_photo = AsyncMock()

    ok = await _service(bot).prewarm_logo()

    assert ok is True
    bot.send_photo.assert_not_awaited()


async def test_prewarm_no_target_chat_skips(monkeypatch):
    monkeypatch.setattr(mp, '_logo_file_id', None, raising=False)
    monkeypatch.setattr(mp, 'get_logo_media', lambda: object())
    monkeypatch.setattr(settings, 'ADMIN_IDS', '')  # no admin fallback either
    bot = MagicMock()
    bot.send_photo = AsyncMock()

    ok = await _service(bot, chat_id=None).prewarm_logo()

    assert ok is False
    bot.send_photo.assert_not_awaited()


async def test_prewarm_is_best_effort_on_timeout(monkeypatch):
    monkeypatch.setattr(mp, '_logo_file_id', None, raising=False)
    monkeypatch.setattr(mp, 'get_logo_media', lambda: object())
    monkeypatch.setattr(settings, 'MONITORING_NOTIFICATION_SEND_TIMEOUT', 0.05)

    async def _hang(**_kwargs):
        await asyncio.sleep(30)

    bot = MagicMock()
    bot.send_photo = _hang

    ok = await asyncio.wait_for(_service(bot).prewarm_logo(), timeout=5)
    assert ok is False  # timed out, swallowed, never raises
