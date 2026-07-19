"""Regression test for Telegram bug #652234 (promo-offer broadcast).

The broadcast committed an offer per recipient and then sent Telegram notifications to
everyone synchronously inside the HTTP request; a large fan-out overran the proxy timeout,
so the cabinet showed an error while offers were already created and notifications kept
going. The fan-out now takes plain (telegram_id, offer_id) tuples — not ORM objects bound
to the request session — so it can run detached as a background task.

This pins the refactored signature/behaviour: it works off plain ids and counts
sent/failed correctly (including skipping recipients without a telegram_id).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import InlineKeyboardButton

from app.cabinet.routes import admin_promo_offers as m


@pytest.mark.asyncio
async def test_send_promo_notifications_works_off_plain_ids(monkeypatch):
    chat_ids: list[int] = []

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=lambda chat_id, text, reply_markup: chat_ids.append(chat_id))
    bot.session = MagicMock()
    bot.session.close = AsyncMock()
    monkeypatch.setattr(m, '_get_bot', lambda: bot)
    # Isolate the fan-out from the keyboard helper (which needs miniapp config).
    monkeypatch.setattr(
        m,
        'build_miniapp_or_callback_button',
        lambda text, callback_data: InlineKeyboardButton(text=text, callback_data=callback_data),
    )

    sent, failed = await m._send_promo_notifications(
        [(111, 1), (222, 2), (0, 3)],  # (telegram_id, offer_id); 0 => email-only, skipped
        message_text='hi',
        button_text=None,
        discount_percent=10,
        bonus_amount_kopeks=0,
        valid_hours=24,
    )

    assert (sent, failed) == (2, 1)
    assert chat_ids == [111, 222]
    bot.session.close.assert_awaited()


@pytest.mark.asyncio
async def test_empty_targets_is_noop(monkeypatch):
    monkeypatch.setattr(m, '_get_bot', lambda: (_ for _ in ()).throw(AssertionError('bot must not be created')))
    assert await m._send_promo_notifications([], None, None, 0, 0, 24) == (0, 0)
