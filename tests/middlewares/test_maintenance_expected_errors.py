"""Ожидаемые отказы Telegram при показе заглушки техработ не дают traceback.

«query is too old» (юзер жмёт кнопку старого сообщения во время техработ),
блокировка бота и недоступный чат — штатный шум: логируются debug-строкой,
а _auto_capture_exc_info не разворачивает traceback (error=str). Неожиданные
BadRequest остаются на error-уровне с traceback.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery

import app.middlewares.maintenance as mw
from app.config import settings


def _mk_callback(answer_error) -> MagicMock:
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=148871030, is_bot=False)
    callback.answer = AsyncMock(side_effect=answer_error)
    return callback


def _activate_maintenance(monkeypatch) -> None:
    monkeypatch.setattr(mw.maintenance_service, 'is_maintenance_active', lambda: True)
    monkeypatch.setattr(mw.maintenance_service, 'get_maintenance_message', lambda: 'техработы')
    monkeypatch.setattr(type(settings), 'is_admin', lambda self, uid: False)
    monkeypatch.setattr(
        'app.services.support_settings_service.SupportSettingsService.is_moderator', staticmethod(lambda uid: False)
    )


async def _run(monkeypatch, answer_error):
    _activate_maintenance(monkeypatch)
    fake_logger = MagicMock()
    monkeypatch.setattr(mw, 'logger', fake_logger)

    middleware = mw.MaintenanceMiddleware()
    handler = AsyncMock()
    result = await middleware(handler, _mk_callback(answer_error), {})

    handler.assert_not_awaited()  # апдейт блокируется в любом случае
    assert result is None
    return fake_logger


async def test_stale_callback_logged_quietly(monkeypatch):
    error = TelegramBadRequest(
        method=None, message='Bad Request: query is too old and response timeout expired or query ID is invalid'
    )
    fake_logger = await _run(monkeypatch, error)

    fake_logger.error.assert_not_called()
    fake_logger.debug.assert_called_once()
    # Исключение передано строкой — traceback не разворачивается
    assert isinstance(fake_logger.debug.call_args.kwargs['error'], str)


async def test_blocked_bot_logged_quietly(monkeypatch):
    error = TelegramForbiddenError(method=None, message='Forbidden: bot was blocked by the user')
    fake_logger = await _run(monkeypatch, error)

    fake_logger.error.assert_not_called()
    fake_logger.debug.assert_called_once()


async def test_unexpected_bad_request_stays_error(monkeypatch):
    error = TelegramBadRequest(method=None, message="Bad Request: can't parse entities")
    fake_logger = await _run(monkeypatch, error)

    fake_logger.error.assert_called_once()
