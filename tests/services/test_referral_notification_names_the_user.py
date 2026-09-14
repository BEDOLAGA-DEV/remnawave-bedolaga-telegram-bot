"""Ошибка доставки реферального уведомления называет пользователя: Telegram ID и username."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramForbiddenError

import app.services.referral_service as referral_mod


@pytest.mark.asyncio
async def test_delivery_failure_log_names_the_user(monkeypatch):
    log = MagicMock()
    monkeypatch.setattr(referral_mod, 'logger', log)
    bot = SimpleNamespace(
        send_message=AsyncMock(
            side_effect=TelegramForbiddenError(method=MagicMock(), message='Forbidden: bot was blocked by the user')
        )
    )
    user = SimpleNamespace(id=7, telegram_id=123456789, username='vasya', email=None)

    await referral_mod.send_referral_notification(bot, 123456789, 'Привет', user=user)

    kwargs = log.error.call_args.kwargs
    assert kwargs['telegram_id'] == 123456789
    assert kwargs['username'] == 'vasya'
