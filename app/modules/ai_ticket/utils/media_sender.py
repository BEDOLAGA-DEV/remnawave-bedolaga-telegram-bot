"""
Утилита для извлечения медиа-тегов [MEDIA:tag] из ответа ИИ
и отправки медиа-вложений пользователю.
"""

import re
from typing import Sequence

import structlog
from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models_ai_ticket import AIFaqMedia

logger = structlog.get_logger(__name__)

# Регулярка: ищет [MEDIA:tag] в тексте (с поддержкой пробелов)
MEDIA_TAG_PATTERN = re.compile(r'\[\s*MEDIA\s*:\s*([a-zA-Z0-9_]+)\s*\]', re.IGNORECASE)


def extract_media_tags(text: str) -> list[str]:
    """Извлекает все уникальные теги из текста."""
    return list(dict.fromkeys(MEDIA_TAG_PATTERN.findall(text)))


def strip_media_tags(text: str) -> str:
    """
    Убирает все [MEDIA:tag] из текста.
    Также пытается убрать пояснительный текст, если ИИ его продублировал:
    например: "- [MEDIA:tag] — название" или просто "[MEDIA:tag] — название"
    """
    # 1. Сначала убираем конструкции типа "- [MEDIA:tag] — описание" или "[MEDIA:tag] — описание"
    # Ищем тег и всё что после него до конца строки, если там есть тире или дефис
    cleaned = re.sub(r'(?:-\s*)?\[\s*MEDIA\s*:[^\]]+\]\s*[—–-]\s*[^\n]+', '', text, flags=re.IGNORECASE)
    
    # 2. Убираем оставшиеся одиночные теги
    cleaned = MEDIA_TAG_PATTERN.sub('', cleaned)
    
    # 3. Чистим лишние переносы
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


async def get_media_by_tags(db: AsyncSession, tags: list[str]) -> list[AIFaqMedia]:
    """Находит медиа-записи по списку тегов."""
    if not tags:
        return []
    stmt = select(AIFaqMedia).where(AIFaqMedia.tag.in_(tags))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def extract_and_send_media(
    bot: Bot,
    chat_id: int,
    ai_response: str,
    db: AsyncSession,
) -> str:
    """
    Основная функция:
    1. Ищет все [MEDIA:tag] в тексте ответа ИИ
    2. Убирает теги из текста
    3. Загружает медиа-записи из БД по тегам
    4. Отправляет каждое медиа отдельным сообщением
    5. Возвращает очищенный текст без тегов
    """
    tags = extract_media_tags(ai_response)
    if not tags:
        return ai_response

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    close_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='❌ Закрыть', callback_data='ai_faq_media_close')]
    ])

    # Убираем теги из текста
    clean_text = strip_media_tags(ai_response)

    # Находим медиа
    media_items = await get_media_by_tags(db, tags)

    # Отправляем каждое медиа
    for media in media_items:
        try:
            caption = media.caption or ''
            if media.media_type == 'photo':
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=media.file_id,
                    caption=caption,
                    parse_mode='HTML',
                    reply_markup=close_kb,
                )
            elif media.media_type == 'video':
                await bot.send_video(
                    chat_id=chat_id,
                    video=media.file_id,
                    caption=caption,
                    parse_mode='HTML',
                    reply_markup=close_kb,
                )
            elif media.media_type == 'animation':
                await bot.send_animation(
                    chat_id=chat_id,
                    animation=media.file_id,
                    caption=caption,
                    parse_mode='HTML',
                    reply_markup=close_kb,
                )
            else:
                await bot.send_document(
                    chat_id=chat_id,
                    document=media.file_id,
                    caption=caption,
                    parse_mode='HTML',
                    reply_markup=close_kb,
                )
            logger.info('ai_media_sender.sent', tag=media.tag, type=media.media_type, chat_id=chat_id)
        except Exception as e:
            logger.error('ai_media_sender.send_failed', tag=media.tag, error=str(e))

    return clean_text
