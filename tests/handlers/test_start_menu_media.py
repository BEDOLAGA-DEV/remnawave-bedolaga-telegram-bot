"""Медиа-шапка стартового меню: видео → фото-логотип → текст.

Видео загружается администратором через кабинет и хранится как Telegram
file_id. Требования: видео вытесняет фото-логотип; длинная подпись (больше
лимита Telegram) уводит на текст; сбой отправки видео не лишает пользователя
меню.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from app.config import settings
from app.database.models import SystemSetting
from app.handlers.start import send_menu_with_media
from app.services import start_media_service as sms
from tests.fixtures.sqlite_memory import memory_session


TABLES = (SystemSetting.__table__,)

KEYBOARD = object()


def _bot() -> AsyncMock:
    bot = AsyncMock()
    bot.send_video = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.send_message = AsyncMock()
    return bot


async def test_video_takes_precedence_over_logo(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sms.reset_start_video_cache()
        monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', True)
        await sms.set_start_video_file_id(db, 'vid-123')

        bot = _bot()
        await send_menu_with_media(bot, 555, 'Меню', KEYBOARD, db)

        bot.send_video.assert_awaited_once()
        assert bot.send_video.await_args.kwargs['video'] == 'vid-123'
        assert bot.send_video.await_args.kwargs['caption'] == 'Меню'
        bot.send_photo.assert_not_awaited()
        bot.send_message.assert_not_awaited()


async def test_without_video_falls_back_to_logo(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sms.reset_start_video_cache()
        monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', True)

        bot = _bot()
        await send_menu_with_media(bot, 555, 'Меню', KEYBOARD, db)

        bot.send_photo.assert_awaited_once()
        bot.send_video.assert_not_awaited()


async def test_removed_video_returns_to_logo(monkeypatch):
    """Удаление видео в кабинете сразу возвращает прежнее поведение."""
    async with memory_session(monkeypatch, TABLES) as db:
        sms.reset_start_video_cache()
        monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', True)
        await sms.set_start_video_file_id(db, 'vid-123')
        await sms.set_start_video_file_id(db, None)

        bot = _bot()
        await send_menu_with_media(bot, 555, 'Меню', KEYBOARD, db)

        bot.send_video.assert_not_awaited()
        bot.send_photo.assert_awaited_once()


async def test_long_caption_goes_to_plain_text(monkeypatch):
    """Подпись длиннее лимита Telegram нельзя приложить ни к видео, ни к фото."""
    async with memory_session(monkeypatch, TABLES) as db:
        sms.reset_start_video_cache()
        monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', True)
        await sms.set_start_video_file_id(db, 'vid-123')

        bot = _bot()
        await send_menu_with_media(bot, 555, 'x' * 1100, KEYBOARD, db)

        bot.send_video.assert_not_awaited()
        bot.send_photo.assert_not_awaited()
        bot.send_message.assert_awaited_once()


async def test_video_send_failure_still_delivers_menu(monkeypatch):
    """Битый file_id не должен оставлять пользователя без меню."""
    async with memory_session(monkeypatch, TABLES) as db:
        sms.reset_start_video_cache()
        monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', True)
        await sms.set_start_video_file_id(db, 'broken')

        bot = _bot()
        bot.send_video = AsyncMock(side_effect=RuntimeError('wrong file identifier'))

        await send_menu_with_media(bot, 555, 'Меню', KEYBOARD, db)

        bot.send_photo.assert_awaited_once()


async def test_video_used_even_when_logo_mode_disabled(monkeypatch):
    """Видео — самостоятельная настройка, не зависит от ENABLE_LOGO_MODE."""
    async with memory_session(monkeypatch, TABLES) as db:
        sms.reset_start_video_cache()
        monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', False)
        await sms.set_start_video_file_id(db, 'vid-123')

        bot = _bot()
        await send_menu_with_media(bot, 555, 'Меню', KEYBOARD, db)

        bot.send_video.assert_awaited_once()
        bot.send_message.assert_not_awaited()


# ---- answer-путь (сама команда /start и меню после регистрации) ----------


def _message() -> AsyncMock:
    message = AsyncMock()
    message.answer = AsyncMock()
    message.answer_video = AsyncMock()
    return message


async def test_answer_path_sends_video(monkeypatch):
    """/start отвечает через message.answer — видео обязано работать и там."""
    from app.handlers.start import answer_menu_with_media

    async with memory_session(monkeypatch, TABLES) as db:
        sms.reset_start_video_cache()
        await sms.set_start_video_file_id(db, 'vid-777')

        message = _message()
        await answer_menu_with_media(message, 'Меню', KEYBOARD, db)

        message.answer_video.assert_awaited_once()
        assert message.answer_video.await_args.kwargs['video'] == 'vid-777'
        message.answer.assert_not_awaited()


async def test_answer_path_without_video_delegates_unchanged(monkeypatch):
    """Без видео поведение обязано остаться ровно прежним (патченный answer)."""
    from app.handlers.start import answer_menu_with_media

    async with memory_session(monkeypatch, TABLES) as db:
        sms.reset_start_video_cache()

        message = _message()
        await answer_menu_with_media(message, 'Меню', KEYBOARD, db)

        message.answer_video.assert_not_awaited()
        message.answer.assert_awaited_once_with('Меню', reply_markup=KEYBOARD, parse_mode='HTML')


async def test_answer_path_falls_back_when_video_broken(monkeypatch):
    from app.handlers.start import answer_menu_with_media

    async with memory_session(monkeypatch, TABLES) as db:
        sms.reset_start_video_cache()
        await sms.set_start_video_file_id(db, 'broken')

        message = _message()
        message.answer_video = AsyncMock(side_effect=RuntimeError('wrong file identifier'))

        await answer_menu_with_media(message, 'Меню', KEYBOARD, db)

        message.answer.assert_awaited_once()


async def test_answer_path_long_caption_delegates_to_text(monkeypatch):
    from app.handlers.start import answer_menu_with_media

    async with memory_session(monkeypatch, TABLES) as db:
        sms.reset_start_video_cache()
        await sms.set_start_video_file_id(db, 'vid-777')

        message = _message()
        await answer_menu_with_media(message, 'x' * 1100, KEYBOARD, db)

        message.answer_video.assert_not_awaited()
        message.answer.assert_awaited_once()
