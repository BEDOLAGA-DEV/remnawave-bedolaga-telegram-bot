"""Поздний answerCallbackQuery — предупреждение, а не исключение.

Telegram ждёт ответ на нажатие кнопки недолго. Обработчик, который сначала создаёт
платёж у провайдера, а `callback.answer()` зовёт в конце, получает «query is too old»;
его собственный `except Exception` считает это ошибкой и шлёт отчёт админам, хотя
работа сделана и пользователь всё увидел. Таких обработчиков по боту полтора десятка,
поэтому лечится не каждый, а исходящий запрос: middleware сессии превращает устаревший
ответ на нажатие в предупреждение. Только для answerCallbackQuery — остальные методы
и остальные ошибки летят дальше как раньше.
"""

from __future__ import annotations

from typing import Any

import structlog
from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, TelegramMethod

from app.utils.telegram_errors import is_stale_callback_query_error


logger = structlog.get_logger(__name__)


class StaleCallbackAnswerMiddleware(BaseRequestMiddleware):
    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[Any],
        bot: Bot,
        method: TelegramMethod[Any],
    ) -> Any:
        try:
            return await make_request(bot, method)
        except TelegramBadRequest as error:
            if not isinstance(method, AnswerCallbackQuery) or not is_stale_callback_query_error(error):
                raise
            logger.warning(
                'Ответ на устаревшее нажатие пропущен',
                callback_query_id=method.callback_query_id,
                text=method.text,
            )
            return True
