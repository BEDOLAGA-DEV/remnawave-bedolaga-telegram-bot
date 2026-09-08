"""Распознавание штатных отказов Telegram Bot API.

Один список фраз на всех: ответ на устаревшее нажатие кнопки Telegram описывает
двумя формулировками, и разрозненные копии этих строк по middleware уже
расходились.
"""

from __future__ import annotations

from typing import Final

from aiogram.exceptions import TelegramBadRequest


STALE_CALLBACK_QUERY_PHRASES: Final[tuple[str, ...]] = (
    'query is too old',
    'query id is invalid',
    'response timeout expired',
)


def is_stale_callback_query_error(error: BaseException) -> bool:
    """Telegram уже не ждёт ответ на это нажатие: пользователь давно увидел результат."""
    if not isinstance(error, TelegramBadRequest):
        return False
    text = str(error).lower()
    return any(phrase in text for phrase in STALE_CALLBACK_QUERY_PHRASES)
