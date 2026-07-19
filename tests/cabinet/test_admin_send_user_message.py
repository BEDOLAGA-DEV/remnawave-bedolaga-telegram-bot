"""Кабинетный аналог бот-кнопки «✉️ Отправить сообщение» в карточке юзера.

POST /admin/users/{user_id}/send-message: шлёт юзеру прямое Telegram-сообщение
через бота. Email-only юзеры (без telegram_id) получают отдельный код ошибки
no_telegram_id — фронт объясняет, а не показывает generic-ошибку. Сессия бота
закрывается в любом исходе.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_users as m
from app.cabinet.schemas.users import SendUserMessageRequest


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.session = MagicMock()
    bot.session.close = AsyncMock()
    return bot


async def test_send_message_success(monkeypatch):
    target = MagicMock(telegram_id=111)
    monkeypatch.setattr(m, 'get_user_by_id', AsyncMock(return_value=target))
    monkeypatch.setattr(type(m.settings), 'BOT_TOKEN', 'token', raising=False)
    bot = _bot()
    monkeypatch.setattr('app.bot_factory.create_bot', lambda: bot)

    result = await m.send_user_message(
        user_id=1,
        request=SendUserMessageRequest(text='  привет  '),
        admin=MagicMock(id=7),
        db=AsyncMock(),
    )

    assert result.success is True
    bot.send_message.assert_awaited_once_with(111, 'привет', parse_mode='HTML')
    bot.session.close.assert_awaited_once()  # сессия закрыта


async def test_send_message_email_only_user_rejected(monkeypatch):
    """Email-only юзер → 400 с кодом no_telegram_id, бот не создаётся."""
    target = MagicMock(telegram_id=None)
    monkeypatch.setattr(m, 'get_user_by_id', AsyncMock(return_value=target))

    with pytest.raises(HTTPException) as exc:
        await m.send_user_message(
            user_id=1,
            request=SendUserMessageRequest(text='hi'),
            admin=MagicMock(id=7),
            db=AsyncMock(),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail['code'] == 'no_telegram_id'


async def test_send_message_forbidden_maps_to_400_and_closes_session(monkeypatch):
    """Юзер заблокировал бота → 400 с кодом forbidden, сессия бота закрыта."""
    from aiogram.exceptions import TelegramForbiddenError

    target = MagicMock(telegram_id=111)
    monkeypatch.setattr(m, 'get_user_by_id', AsyncMock(return_value=target))
    monkeypatch.setattr(type(m.settings), 'BOT_TOKEN', 'token', raising=False)
    bot = _bot()
    bot.send_message = AsyncMock(side_effect=TelegramForbiddenError(method=MagicMock(), message='blocked'))
    monkeypatch.setattr('app.bot_factory.create_bot', lambda: bot)

    with pytest.raises(HTTPException) as exc:
        await m.send_user_message(
            user_id=1,
            request=SendUserMessageRequest(text='hi'),
            admin=MagicMock(id=7),
            db=AsyncMock(),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail['code'] == 'forbidden'
    bot.session.close.assert_awaited_once()


async def test_send_message_user_not_found(monkeypatch):
    monkeypatch.setattr(m, 'get_user_by_id', AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as exc:
        await m.send_user_message(
            user_id=404,
            request=SendUserMessageRequest(text='hi'),
            admin=MagicMock(id=7),
            db=AsyncMock(),
        )

    assert exc.value.status_code == 404


def test_send_message_permission_registered():
    """users:send_message должен существовать в реестре RBAC — иначе
    require_permission молча зарежет всех не-legacy админов."""
    from app.services.permission_service import get_all_permissions

    assert 'users:send_message' in get_all_permissions()
