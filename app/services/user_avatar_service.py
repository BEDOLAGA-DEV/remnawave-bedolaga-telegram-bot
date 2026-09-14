"""Фото профиля Telegram для шапки кабинета.

Кабинет раньше брал аватар только из ``photo_url`` в initData Mini App: нет поля
или картинка не загрузилась — остаётся серый круг, а при входе с сайта
(виджет, OIDC, почта) фото не появлялось никогда. Бот может спросить Telegram сам:
``getUserProfilePhotos`` работает для любого, кто писал боту, с учётом настроек
приватности пользователя. Отдаём file_id, ссылку подписывает прокси медиа.
"""

from __future__ import annotations

import structlog
from aiogram import Bot
from aiogram.types import UserProfilePhotos

from app.utils.cache import cache, cache_key


logger = structlog.get_logger(__name__)

# Шапка рисует круг 40px; 160px хватает и для retina, а трафика вчетверо меньше,
# чем у полноразмерного фото.
AVATAR_MIN_SIDE_PX = 160
# Фото профиля меняют редко; час — компромисс между свежестью и запросами к Bot API.
AVATAR_CACHE_TTL_SECONDS = 60 * 60

# В кеше пустая строка = «фото нет», чтобы не спрашивать Telegram на каждый показ.
_NO_PHOTO = ''


def pick_avatar_file_id(photos: UserProfilePhotos) -> str | None:
    """Самый маленький размер, который ещё не мылится в шапке; иначе самый крупный."""
    if not photos.photos or not photos.photos[0]:
        return None
    sizes = sorted(photos.photos[0], key=lambda size: size.width)
    sharp_enough = next((size for size in sizes if size.width >= AVATAR_MIN_SIDE_PX), None)
    return (sharp_enough or sizes[-1]).file_id


async def get_avatar_file_id(bot: Bot, telegram_id: int) -> str | None:
    """file_id текущего фото профиля или None. Никогда не бросает: аватар — не повод ронять кабинет."""
    key = cache_key('user_avatar', telegram_id)
    cached = await cache.get(key)
    if cached is not None:
        return cached or None

    try:
        photos = await bot.get_user_profile_photos(user_id=telegram_id, limit=1)
    except Exception as error:
        logger.warning('Не удалось получить фото профиля', telegram_id=telegram_id, error=str(error)[:200])
        return None

    file_id = pick_avatar_file_id(photos)
    await cache.set(key, file_id or _NO_PHOTO, expire=AVATAR_CACHE_TTL_SECONDS)
    return file_id
