"""Аватар пользователя для шапки кабинета отдаёт сам бот.

Жалоба: в Mini App вместо фото Telegram — серый круг. Кабинет брал картинку
только из photo_url в initData, и при любой осечке (нет поля, картинка не
загрузилась, вход не из Telegram) оставался с заглушкой. Теперь у бота есть
GET /cabinet/auth/me/avatar: фото профиля через Bot API, подписанная ссылка
через уже существующий прокси медиа, кеш на час.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import PhotoSize, UserProfilePhotos

from app.cabinet.routes.auth import get_my_avatar
from app.services import user_avatar_service


def _size(file_id: str, width: int) -> PhotoSize:
    return PhotoSize(file_id=file_id, file_unique_id=f'u{file_id}', width=width, height=width)


class _FakeCache:
    def __init__(self) -> None:
        self.store: dict[str, object] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value, expire=None) -> bool:
        self.store[key] = value
        return True


class _BotContext:
    """create_bot() используется как async-контекст — имитируем ровно это."""

    def __init__(self, bot) -> None:
        self.bot = bot

    async def __aenter__(self):
        return self.bot

    async def __aexit__(self, *_exc) -> None:
        return None


def test_picks_the_smallest_size_that_is_still_sharp() -> None:
    photos = UserProfilePhotos(total_count=1, photos=[[_size('tiny', 90), _size('mid', 160), _size('big', 640)]])
    assert user_avatar_service.pick_avatar_file_id(photos) == 'mid'


def test_falls_back_to_the_largest_when_all_are_small() -> None:
    photos = UserProfilePhotos(total_count=1, photos=[[_size('tiny', 90), _size('small', 120)]])
    assert user_avatar_service.pick_avatar_file_id(photos) == 'small'


def test_no_photos_means_no_avatar() -> None:
    assert user_avatar_service.pick_avatar_file_id(UserProfilePhotos(total_count=0, photos=[])) is None


async def test_file_id_is_cached_including_the_absence_of_a_photo(monkeypatch) -> None:
    cache = _FakeCache()
    monkeypatch.setattr(user_avatar_service, 'cache', cache)
    bot = MagicMock()
    bot.get_user_profile_photos = AsyncMock(return_value=UserProfilePhotos(total_count=0, photos=[]))

    assert await user_avatar_service.get_avatar_file_id(bot, 123) is None
    assert await user_avatar_service.get_avatar_file_id(bot, 123) is None
    bot.get_user_profile_photos.assert_awaited_once()


async def test_telegram_error_does_not_break_the_cabinet(monkeypatch) -> None:
    monkeypatch.setattr(user_avatar_service, 'cache', _FakeCache())
    bot = MagicMock()
    bot.get_user_profile_photos = AsyncMock(
        side_effect=TelegramBadRequest(method=None, message='Bad Request: user not found')
    )

    assert await user_avatar_service.get_avatar_file_id(bot, 123) is None


async def test_route_returns_signed_media_url(monkeypatch) -> None:
    monkeypatch.setattr(user_avatar_service, 'cache', _FakeCache())
    bot = MagicMock()
    bot.get_user_profile_photos = AsyncMock(
        return_value=UserProfilePhotos(total_count=1, photos=[[_size('A' * 32, 320)]])
    )
    request = MagicMock()
    request.url_for.return_value = 'https://api.example/cabinet/media/' + 'A' * 32

    with patch('app.bot_factory.create_bot', return_value=_BotContext(bot)):
        response = await get_my_avatar(request=request, user=SimpleNamespace(id=1, telegram_id=123))

    assert response.photo_url is not None
    assert response.photo_url.startswith('https://api.example/cabinet/media/' + 'A' * 32)
    assert 'token=' in response.photo_url


async def test_route_without_telegram_account_returns_nothing() -> None:
    response = await get_my_avatar(request=MagicMock(), user=SimpleNamespace(id=1, telegram_id=None))
    assert response.photo_url is None
