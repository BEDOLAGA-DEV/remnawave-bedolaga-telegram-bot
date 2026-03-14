import asyncio

import structlog
from aiogram import types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.types import InaccessibleMessage, InputMediaPhoto

from app.config import settings

from .message_patch import (
    LOGO_PATH,
    _cache_logo_file_id,
    append_privacy_hint,
    caption_exceeds_telegram_limit,
    get_logo_media,
    is_privacy_restricted_error,
    is_qr_message,
    prepare_privacy_safe_kwargs,
)


logger = structlog.get_logger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 0.5


def _resolve_media(message: types.Message):
    if isinstance(message, InaccessibleMessage):
        return get_logo_media()
    if settings.ENABLE_LOGO_MODE and not is_qr_message(message):
        return get_logo_media()
    if message.photo and not is_qr_message(message):
        return message.photo[-1].file_id
    return get_logo_media()


def _get_language(callback: types.CallbackQuery) -> str | None:
    try:
        user = callback.from_user
        if user and getattr(user, 'language_code', None):
            return user.language_code
    except AttributeError:
        pass
    return None


def _build_base_kwargs(keyboard: types.InlineKeyboardMarkup | None, parse_mode: str | None):
    kwargs: dict[str, object] = {}
    if parse_mode is not None:
        kwargs['parse_mode'] = parse_mode
    if keyboard is not None:
        kwargs['reply_markup'] = keyboard
    return kwargs


async def _answer_text(
    callback: types.CallbackQuery,
    caption: str,
    keyboard: types.InlineKeyboardMarkup | None,
    parse_mode: str | None,
    error: TelegramBadRequest | None = None,
) -> None:
    language = _get_language(callback)
    kwargs = _build_base_kwargs(keyboard, parse_mode)

    if error and is_privacy_restricted_error(error):
        caption = append_privacy_hint(caption, language)
        kwargs = prepare_privacy_safe_kwargs(kwargs)

    kwargs.setdefault('parse_mode', parse_mode or 'HTML')

    await callback.message.answer(
        caption,
        **kwargs,
    )


async def edit_or_answer_photo(
    callback: types.CallbackQuery,
    caption: str,
    keyboard: types.InlineKeyboardMarkup,
    parse_mode: str | None = 'HTML',
    *,
    force_text: bool = False,
) -> None:
    resolved_parse_mode = parse_mode or 'HTML'

    # Если сообщение недоступно, отправляем новое сообщение
    if isinstance(callback.message, InaccessibleMessage):
        try:
            if settings.ENABLE_LOGO_MODE and LOGO_PATH.exists():
                result = await callback.message.answer_photo(
                    photo=get_logo_media(),
                    caption=caption,
                    reply_markup=keyboard,
                    parse_mode=resolved_parse_mode,
                )
                _cache_logo_file_id(result)
            else:
                await callback.message.answer(
                    caption,
                    reply_markup=keyboard,
                    parse_mode=resolved_parse_mode,
                )
        except Exception as e:
            logger.warning('Не удалось отправить новое сообщение для InaccessibleMessage', e=e)
            try:
                await callback.message.answer(
                    caption,
                    reply_markup=keyboard,
                    parse_mode=resolved_parse_mode,
                )
            except Exception:
                pass
        return

    # Если режим логотипа выключен или требуется текстовое сообщение — работаем текстом
    if force_text or not settings.ENABLE_LOGO_MODE:
        try:
            if callback.message.photo:
                await callback.message.delete()
                await _answer_text(callback, caption, keyboard, resolved_parse_mode)
            else:
                await callback.message.edit_text(
                    caption,
                    reply_markup=keyboard,
                    parse_mode=resolved_parse_mode,
                )
        except TelegramForbiddenError:
            logger.debug('Пользователь заблокировал бота, пропускаем')
        except TelegramBadRequest as error:
            try:
                await callback.message.delete()
            except Exception:
                pass
            await _answer_text(callback, caption, keyboard, resolved_parse_mode, error)
        return

    # Если текст слишком длинный для caption — отправим как текст
    if caption_exceeds_telegram_limit(caption):
        try:
            if callback.message.photo:
                await callback.message.delete()
            await _answer_text(callback, caption, keyboard, resolved_parse_mode)
        except TelegramForbiddenError:
            logger.debug('Пользователь заблокировал бота, пропускаем')
        except TelegramBadRequest as error:
            await _answer_text(callback, caption, keyboard, resolved_parse_mode, error)
        return

    media = _resolve_media(callback.message)

    # Retry logic для сетевых ошибок
    for attempt in range(MAX_RETRIES):
        try:
            await callback.message.edit_media(
                InputMediaPhoto(media=media, caption=caption, parse_mode=(parse_mode or 'HTML')),
                reply_markup=keyboard,
            )
            return  # Успешно — выходим
        except TelegramNetworkError as net_error:
            if attempt < MAX_RETRIES - 1:
                logger.warning(
                    'Сетевая ошибка edit_media (попытка /)',
                    attempt=attempt + 1,
                    MAX_RETRIES=MAX_RETRIES,
                    net_error=net_error,
                )
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                continue
            logger.error('Сетевая ошибка edit_media после попыток', MAX_RETRIES=MAX_RETRIES, net_error=net_error)
            # После всех попыток — фоллбек на текст
            try:
                await callback.message.delete()
            except Exception:
                pass
            await _answer_text(callback, caption, keyboard, resolved_parse_mode)
            return
        except TelegramForbiddenError:
            # Пользователь заблокировал бота — молча игнорируем
            logger.debug('Пользователь заблокировал бота, пропускаем edit_media')
            return
        except TelegramBadRequest as error:
            if is_privacy_restricted_error(error):
                try:
                    await callback.message.delete()
                except Exception:
                    pass
                await _answer_text(callback, caption, keyboard, resolved_parse_mode, error)
                return
            # Фоллбек: если не удалось обновить фото — отправим текст
            try:
                await callback.message.delete()
            except Exception:
                pass
            try:
                # Отправим как фото с логотипом
                result = await callback.message.answer_photo(
                    photo=get_logo_media(),
                    caption=caption,
                    reply_markup=keyboard,
                    parse_mode=resolved_parse_mode,
                )
                _cache_logo_file_id(result)
            except (TelegramBadRequest, TelegramForbiddenError) as photo_error:
                await _answer_text(callback, caption, keyboard, resolved_parse_mode, photo_error)
            except Exception:
                # Последний фоллбек — обычный текст
                await _answer_text(callback, caption, keyboard, resolved_parse_mode)
            return
