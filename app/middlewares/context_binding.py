from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from structlog.contextvars import bound_contextvars

from app.services.runtime_context import bind_current_bot_id


class ContextVarsMiddleware(BaseMiddleware):
    """Bind user/chat context to structlog contextvars for automatic log enrichment."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        ctx: dict[str, Any] = {}
        if hasattr(event, 'from_user') and event.from_user:
            ctx['user_id'] = event.from_user.id
            ctx['username'] = event.from_user.username or ''
        if hasattr(event, 'chat') and event.chat:
            ctx['chat_id'] = event.chat.id
        bot = data.get('bot')
        bot_id = getattr(bot, 'id', None)
        with bind_current_bot_id(bot_id):
            with bound_contextvars(**ctx):
                return await handler(event, data)
