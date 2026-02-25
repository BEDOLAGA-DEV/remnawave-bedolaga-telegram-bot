from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery, TelegramObject, User as TgUser

from app.localization.loader import DEFAULT_LANGUAGE
from app.localization.texts import get_texts
from app.services.blacklist_service import blacklist_service


logger = structlog.get_logger(__name__)


class BlacklistMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: TgUser | None = None
        if isinstance(event, (Message, CallbackQuery, PreCheckoutQuery)):
            user = event.from_user

        if not user or user.is_bot:
            return await handler(event, data)

        language = DEFAULT_LANGUAGE
        db_user = data.get('db_user')
        if db_user and getattr(db_user, 'language', None):
            language = db_user.language
        elif user.language_code:
            language = user.language_code.split('-')[0]
        texts = get_texts(language)

        is_blacklisted, reason = await blacklist_service.is_user_blacklisted(user.id, user.username)

        if not is_blacklisted:
            return await handler(event, data)

        logger.warning('🚫 Пользователь (@) из черного списка', user_id=user.id, username=user.username, reason=reason)

        block_text = texts.t(
            'BLACKLIST_ACCESS_DENIED_WITH_REASON',
            '🚫 Доступ запрещен\n\nПричина: {reason}\n\nЕсли вы считаете, что это ошибка, обратитесь в поддержку.',
        ).format(reason=reason)

        try:
            if isinstance(event, Message):
                await event.answer(block_text)
            elif isinstance(event, CallbackQuery):
                await event.answer(block_text, show_alert=True)
            elif isinstance(event, PreCheckoutQuery):
                await event.answer(
                    ok=False,
                    error_message=texts.t(
                        'BLACKLIST_ACCESS_DENIED_SHORT',
                        'Доступ запрещен',
                    ),
                )
        except Exception as e:
            logger.error('Ошибка отправки сообщения о блокировке пользователю', user_id=user.id, error=e)

        return None
