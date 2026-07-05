"""Session-middleware: премиум-эмодзи в inline-кнопках исходящих запросов.

Перехватывает каждый исходящий вызов Bot API. Если у метода есть
reply_markup с InlineKeyboardMarkup — ведущие эмодзи кнопок заменяются на
icon_custom_emoji_id (маппинг data/premium_emoji.json). Регистрируется на
bot.session в app/bot_factory.py, поэтому покрывает все пути отправки:
хендлеры, сервисы, рассылки, прямые bot.send_message / edit_reply_markup.
"""

from __future__ import annotations

import structlog
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.types import InlineKeyboardMarkup

from app.utils.premium_emoji import apply_premium_emoji_to_markup

logger = structlog.get_logger(__name__)


class PremiumEmojiRequestMiddleware(BaseRequestMiddleware):
    """Подменяет reply_markup исходящих методов на версию с премиум-иконками."""

    async def __call__(self, make_request, bot, method):
        try:
            markup = getattr(method, 'reply_markup', None)
            if isinstance(markup, InlineKeyboardMarkup):
                new_markup = apply_premium_emoji_to_markup(markup)
                if new_markup is not markup:
                    method = method.model_copy(update={'reply_markup': new_markup})
        except Exception as exc:
            # Косметика никогда не должна ломать исходящий запрос
            logger.warning(
                'Premium emoji markup transform failed, sending original',
                error=str(exc),
            )
        return await make_request(bot, method)
