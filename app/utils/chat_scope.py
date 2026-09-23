"""Откуда пришло нажатие: личка или групповой чат.

Карточки, которые бот кладёт в групповой админ-чат, после действия должны
перерисовываться групповой клавиатурой, а не экраном личной админки.
"""

from __future__ import annotations

from aiogram.enums import ChatType
from aiogram.types import CallbackQuery


def callback_from_group(callback: CallbackQuery) -> bool:
    """Сообщение с кнопкой лежит не в личке (группа, супергруппа, канал)."""
    chat = getattr(getattr(callback, 'message', None), 'chat', None)
    return chat is not None and getattr(chat, 'type', None) != ChatType.PRIVATE
