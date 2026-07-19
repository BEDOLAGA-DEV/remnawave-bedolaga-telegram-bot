"""MonitoringService._send_message_with_logo must not let a stuck Telegram send
block the whole monitoring cycle. A send that hangs past
MONITORING_NOTIFICATION_SEND_TIMEOUT is bounded by asyncio.wait_for: the recipient
is skipped (returns None) and the cycle continues, instead of stalling for the
full 60s session timeout per recipient.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from app.config import settings
from app.services.monitoring_service import MonitoringService


async def _hang(*_args, **_kwargs):
    await asyncio.sleep(30)  # far longer than the test's tiny timeout


def _service(bot) -> MonitoringService:
    return MonitoringService(bot=bot)


async def test_text_send_times_out_and_skips(monkeypatch):
    # Settings fields are patched on the instance (pydantic).
    monkeypatch.setattr(settings, 'MONITORING_NOTIFICATION_SEND_TIMEOUT', 0.05)
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', False)  # take the text-only path

    bot = MagicMock()
    bot.send_message = _hang

    svc = _service(bot)

    result = await asyncio.wait_for(
        svc._send_message_with_logo(chat_id=123, text='hi'),
        timeout=5,  # the inner 0.05s timeout must fire well before this guard
    )
    assert result is None


async def test_photo_send_times_out_and_skips(monkeypatch):
    monkeypatch.setattr(settings, 'MONITORING_NOTIFICATION_SEND_TIMEOUT', 0.05)
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', True)

    # Force the logo branch: pretend the logo file exists and caption fits.
    monkeypatch.setattr('app.services.monitoring_service.LOGO_PATH', MagicMock(exists=lambda: True))
    monkeypatch.setattr('app.services.monitoring_service.caption_exceeds_telegram_limit', lambda _text: False)
    monkeypatch.setattr('app.utils.message_patch.get_logo_media', lambda: 'file_id_stub')

    bot = MagicMock()
    bot.send_photo = _hang
    bot.send_message = _hang  # text fallback must also be bounded

    svc = _service(bot)

    result = await asyncio.wait_for(
        svc._send_message_with_logo(chat_id=123, text='hi'),
        timeout=5,
    )
    assert result is None
