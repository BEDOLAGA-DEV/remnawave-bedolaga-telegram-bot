"""Вложения тикетов, которые бот принимает и показывает в Telegram.

Кабинет и webapi принимают в тикетах фото, видео и документы
(`app/cabinet/schemas/tickets.py`), а Telegram-хендлеры бота распознавали только
фото: видео без подписи отвергалось как «слишком короткий ответ», видео с
подписью сохранялось одной подписью без файла, а при просмотре тикета видео не
показывалось вовсе. Здесь собраны все проверки типа вложения, чтобы хендлеры
пользователя и поддержки не расходились.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from aiogram import types


# Типы одиночных вложений, которые бот принимает из чата и показывает обратно.
_MEDIA_LABELS = {'photo': 'фото', 'video': 'видео'}


def extract_ticket_media(message: types.Message) -> tuple[str | None, str | None, str | None]:
    """Тип, file_id и подпись вложения из сообщения Telegram; без вложения — три None."""
    if message.photo:
        return 'photo', message.photo[-1].file_id, message.caption
    if message.video:
        return 'video', message.video.file_id, message.caption
    return None, None, None


def has_ticket_media(msg: Any) -> bool:
    """Есть ли у сообщения тикета вложение, которое бот умеет показать."""
    return bool(getattr(msg, 'has_media', False) and getattr(msg, 'media_type', None) in _MEDIA_LABELS)


def ticket_media_label(media_type: str | None) -> str:
    """Строка «📎 Вложение: …» для карточки тикета; пустая, если показывать нечего."""
    label = _MEDIA_LABELS.get(media_type or '')
    return f'📎 Вложение: {label}\n' if label else ''


def build_ticket_media_group(messages: Iterable[Any]) -> list[types.InputMediaPhoto | types.InputMediaVideo]:
    """Вложения тикета для send_media_group; фото и видео Telegram разрешает смешивать."""
    media: list[types.InputMediaPhoto | types.InputMediaVideo] = []
    for msg in messages:
        if not has_ticket_media(msg) or not msg.media_file_id:
            continue
        if msg.media_type == 'video':
            media.append(types.InputMediaVideo(media=msg.media_file_id))
        else:
            media.append(types.InputMediaPhoto(media=msg.media_file_id))
    return media
