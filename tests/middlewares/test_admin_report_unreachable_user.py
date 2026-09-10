"""Отчёт админам о недоставленном сообщении — по-человечески, без traceback.

Лог владельца 2026-09-10: реферальное уведомление пользователю, который заблокировал
бота, улетало в админ-чат полным traceback'ом из aiogram. Процессор на error-уровне
сам подтягивает sys.exc_info(); теперь для ожидаемых отказов доставки он вместо
трейса пишет причину и кого не удалось уведомить (Telegram ID, username).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramForbiddenError

import app.middlewares.global_error as ge
from app.logging_handler import TelegramNotifierProcessor


def _caught_forbidden() -> tuple:
    try:
        raise TelegramForbiddenError(method=MagicMock(), message='Forbidden: bot was blocked by the user')
    except TelegramForbiddenError as error:
        return (type(error), error, error.__traceback__)


@pytest.mark.asyncio
async def test_unreachable_user_report_has_reason_and_user_instead_of_traceback(monkeypatch):
    sent = AsyncMock(return_value='sent')
    monkeypatch.setattr(ge, 'send_error_to_admin_chat', sent)
    event = {
        'event': '❌ Ошибка отправки уведомления пользователю',
        'logger': 'app.services.referral_service',
        'level': 'error',
        'telegram_id': 123456789,
        'username': 'vasya',
        'exc_info': _caught_forbidden(),
    }

    await TelegramNotifierProcessor._send(MagicMock(), event, None)

    sent.assert_awaited_once()
    _bot, error, context = sent.await_args.args
    tb_override = sent.await_args.kwargs['tb_override']
    assert type(error).__name__ == 'TelegramForbiddenError'
    assert 'Traceback' not in tb_override and 'File "' not in tb_override
    assert 'заблокировал бота' in tb_override
    assert '123456789' in context and '@vasya' in context


@pytest.mark.asyncio
async def test_real_errors_still_carry_the_traceback(monkeypatch):
    sent = AsyncMock(return_value='sent')
    monkeypatch.setattr(ge, 'send_error_to_admin_chat', sent)
    try:
        raise ValueError('boom')
    except ValueError as error:
        exc_info = (type(error), error, error.__traceback__)
    event = {'event': 'сломалось', 'logger': 'app.x', 'level': 'error', 'exc_info': exc_info}

    await TelegramNotifierProcessor._send(MagicMock(), event, None)

    assert 'Traceback (most recent call last)' in sent.await_args.kwargs['tb_override']
