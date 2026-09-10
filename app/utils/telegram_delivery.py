"""Ожидаемые отказы Telegram при доставке сообщения пользователю.

Бот заблокирован, диалога с ботом ещё не было, аккаунт удалён, чат не найден —
сообщение физически некуда доставить, и это не ошибка бота. Один классификатор
на всех: раньше channel_checker, maintenance и глобальный обработчик держали
свои списки маркеров, а места без списка (реферальные уведомления) слали
админам полный traceback на каждого заблокировавшего бота.
"""

from __future__ import annotations

from typing import Final

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError


BOT_BLOCKED_PHRASE: Final[str] = 'bot was blocked'
NO_DIALOG_PHRASE: Final[str] = "can't initiate conversation"
USER_DEACTIVATED_PHRASE: Final[str] = 'user is deactivated'
CHAT_NOT_FOUND_PHRASE: Final[str] = 'chat not found'

# Маркеры в тексте 400 Bad Request, означающие то же, что 403: писать некому.
UNREACHABLE_USER_PHRASES: Final[tuple[str, ...]] = (
    BOT_BLOCKED_PHRASE,
    NO_DIALOG_PHRASE,
    USER_DEACTIVATED_PHRASE,
    CHAT_NOT_FOUND_PHRASE,
)

GENERIC_UNREACHABLE_REASON: Final[str] = 'пользователь заблокировал бота или диалога с ботом ещё не было'

_REASONS: Final[tuple[tuple[str, str], ...]] = (
    (BOT_BLOCKED_PHRASE, 'пользователь заблокировал бота'),
    (NO_DIALOG_PHRASE, 'пользователь ещё не начинал диалог с ботом'),
    (USER_DEACTIVATED_PHRASE, 'аккаунт пользователя удалён'),
    (CHAT_NOT_FOUND_PHRASE, 'чат не найден: пользователь не начинал диалог с ботом или указан неверный id'),
)


def is_user_unreachable(error: BaseException) -> bool:
    """Сообщение некуда доставить: 403 от Telegram или 400 с маркером недоступности."""
    if isinstance(error, TelegramForbiddenError):
        return True
    if isinstance(error, TelegramBadRequest):
        text = str(error).lower()
        return any(phrase in text for phrase in UNREACHABLE_USER_PHRASES)
    return False


def describe_unreachable(error: BaseException) -> str:
    """Причина по-русски для отчёта админам; для незнакомого отказа — общая формулировка."""
    text = str(error).lower()
    for phrase, reason in _REASONS:
        if phrase in text:
            return reason
    return GENERIC_UNREACHABLE_REASON
