from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, Message, TelegramObject, User as TgUser

from app.config import settings
from app.services.maintenance_service import maintenance_service


logger = structlog.get_logger(__name__)

# Ожидаемые отказы Telegram при показе заглушки техработ: устаревший callback
# (юзер жмёт кнопки старого сообщения, апдейт долежал в очереди дольше лимита
# ответа), удалённый аккаунт, недоступный чат. Это штатный шум — без traceback.
_EXPECTED_NOTIFY_ERRORS = (
    'query is too old',
    'query id is invalid',
    'chat not found',
    'user is deactivated',
)


class MaintenanceMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: TgUser = None
        if isinstance(event, (Message, CallbackQuery)):
            user = event.from_user

        if not user or user.is_bot:
            return await handler(event, data)

        if not maintenance_service.is_maintenance_active():
            return await handler(event, data)

        if settings.is_admin(user.id):
            return await handler(event, data)

        # Moderators are support staff and must keep handling tickets during
        # maintenance (e.g. the reply/block FSM flows started from the new
        # ticket-notification buttons, issue #2988). Same in-memory cache check
        # as resolve_recipient_role — no I/O on the hot path.
        from app.services.support_settings_service import SupportSettingsService

        if SupportSettingsService.is_moderator(user.id):
            return await handler(event, data)

        maintenance_message = maintenance_service.get_maintenance_message()

        try:
            if isinstance(event, Message):
                await event.answer(maintenance_message, parse_mode='HTML')
            elif isinstance(event, CallbackQuery):
                await event.answer(maintenance_message, show_alert=True)
        except TelegramForbiddenError as e:
            logger.debug('Сообщение о техработах не доставлено: бот заблокирован', user_id=user.id, error=str(e))
        except TelegramBadRequest as e:
            if any(marker in str(e).lower() for marker in _EXPECTED_NOTIFY_ERRORS):
                logger.debug(
                    'Сообщение о техработах не доставлено (устаревший callback/недоступный чат)',
                    user_id=user.id,
                    error=str(e),
                )
            else:
                logger.error('Ошибка отправки сообщения о техработах пользователю', user_id=user.id, error=e)
        except Exception as e:
            logger.error('Ошибка отправки сообщения о техработах пользователю', user_id=user.id, error=e)

        logger.info('🔧 Пользователь заблокирован во время техработ', user_id=user.id)
        return None
