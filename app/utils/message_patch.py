from pathlib import Path
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InaccessibleMessage, InputMediaPhoto, Message

from app.config import settings
from app.localization.texts import get_texts

LOGO_PATH = Path(settings.LOGO_FILE)

LOGO_FILE_ID: str | None = None;
def get_logo_file() -> FSInputFile | str:
    global LOGO_FILE_ID;
    if LOGO_FILE_ID:
        return LOGO_FILE_ID;

    return FSInputFile(LOGO_PATH);

def update_logo_file_id(file_id_or_fs: str | FSInputFile):
    global LOGO_FILE_ID;
    if isinstance(file_id_or_fs, FSInputFile):
        return;

    LOGO_FILE_ID = file_id_or_fs;

_PRIVACY_RESTRICTED_CODE = 'BUTTON_USER_PRIVACY_RESTRICTED'
_TOPIC_REQUIRED_ERRORS = (
    'topic must be specified',
    'TOPIC_CLOSED',
    'TOPIC_DELETED',
    'FORUM_CLOSED',
)


def is_qr_message(message: Message) -> bool:
    if isinstance(message, InaccessibleMessage):
        return False
    return bool(message.caption and message.caption.startswith('\U0001f517 Ваша реферальная ссылка'))


_original_answer = Message.answer
_original_edit_text = Message.edit_text


def _get_language(message: Message) -> str | None:
    try:
        user = message.from_user
        if user and getattr(user, 'language_code', None):
            return user.language_code
    except AttributeError:
        pass
    return None


def _default_privacy_hint(language: str | None) -> str:
    if language and language.lower().startswith('en'):
        return (
            '⚠️ Telegram blocked the contact request button because of your privacy settings. '
            'Please allow sharing your contact information or send the required details manually.'
        )
    return (
        '⚠️ Telegram запретил кнопку запроса контакта из-за настроек приватности. '
        'Разрешите отправку контакта в настройках Telegram или отправьте данные вручную.'
    )


def append_privacy_hint(text: str | None, language: str | None) -> str:
    base_text = text or ''
    try:
        hint = get_texts(language).t(
            'PRIVACY_RESTRICTED_BUTTON_HINT',
            default=_default_privacy_hint(language),
        )
    except Exception:
        hint = _default_privacy_hint(language)

    hint = hint.strip()
    if not hint:
        return base_text

    if hint in base_text:
        return base_text

    if base_text:
        return f'{base_text}\n\n{hint}'
    return hint


def prepare_privacy_safe_kwargs(kwargs: dict[str, Any] | None = None) -> dict[str, Any]:
    safe_kwargs: dict[str, Any] = dict(kwargs or {})
    safe_kwargs.pop('reply_markup', None)
    return safe_kwargs


def is_privacy_restricted_error(error: Exception) -> bool:
    if not isinstance(error, TelegramBadRequest):
        return False

    message = getattr(error, 'message', '') or ''
    description = str(error)
    return _PRIVACY_RESTRICTED_CODE in message or _PRIVACY_RESTRICTED_CODE in description


def is_topic_required_error(error: Exception) -> bool:
    """Проверяет, является ли ошибка связанной с топиками/форумами."""
    if not isinstance(error, TelegramBadRequest):
        return False

    description = str(error).lower()
    return any(err.lower() in description for err in _TOPIC_REQUIRED_ERRORS)


async def _answer_with_photo(self: Message, text: str = None, **kwargs):
    # Уважаем флаг в рантайме: если логотип выключен — не подменяем ответ
    if not settings.ENABLE_LOGO_MODE:
        return await _original_answer(self, text, **kwargs)
    # Если caption слишком длинный для фото — отправим как текст
    try:
        if text is not None and len(text) > 900:
            return await _original_answer(self, text, **kwargs)
    except Exception:
        pass
    language = _get_language(self)

    if LOGO_PATH.exists():
        try:
            # Отправляем caption как есть; при ошибке парсинга ниже сработает фоллбек
            msg_ = await self.answer_photo(get_logo_file(), caption=text, **kwargs);
            update_logo_file_id(msg_.photo[-1].file_id)
            return msg_;
        except TelegramBadRequest as error:
            if is_topic_required_error(error):
                # Канал с топиками — просто игнорируем, нельзя ответить без message_thread_id
                return None
            if is_privacy_restricted_error(error):
                fallback_text = append_privacy_hint(text, language)
                safe_kwargs = prepare_privacy_safe_kwargs(kwargs)
                try:
                    return await _original_answer(self, fallback_text, **safe_kwargs)
                except TelegramBadRequest as inner_error:
                    if is_topic_required_error(inner_error):
                        return None
                    raise
            # Фоллбек, если Telegram ругается на caption или другое ограничение: отправим как текст
            try:
                return await _original_answer(self, text, **kwargs)
            except TelegramBadRequest as inner_error:
                if is_topic_required_error(inner_error):
                    return None
                raise
        except Exception:
            try:
                return await _original_answer(self, text, **kwargs)
            except TelegramBadRequest as inner_error:
                if is_topic_required_error(inner_error):
                    return None
                raise
    try:
        return await _original_answer(self, text, **kwargs)
    except TelegramBadRequest as error:
        if is_topic_required_error(error):
            return None
        raise


async def _edit_with_photo(self: Message, text: str, **kwargs):
    # Уважаем флаг в рантайме: если логотип выключен — не подменяем редактирование
    if not settings.ENABLE_LOGO_MODE:
        return await _original_edit_text(self, text, **kwargs)
    if self.photo:
        language = _get_language(self)
        # Если caption потенциально слишком длинный — отправим как текст вместо caption
        try:
            if text is not None and len(text) > 900:
                try:
                    await self.delete()
                except Exception:
                    pass
                return await _original_answer(self, text, **kwargs)
        except Exception:
            pass
        # Всегда используем логотип если включен режим логотипа,
        # кроме специальных случаев (QR сообщения)
        if (settings.ENABLE_LOGO_MODE and LOGO_PATH.exists() and not is_qr_message(self)) or (
            is_qr_message(self) and LOGO_PATH.exists()
        ):
            media = get_logo_file();
        else:
            media = self.photo[-1].file_id
        media_kwargs = {'media': media, 'caption': text}
        edit_kwargs = dict(kwargs)
        if 'parse_mode' in edit_kwargs:
            _pm = edit_kwargs.pop('parse_mode')
            media_kwargs['parse_mode'] = _pm if _pm is not None else 'HTML'
        else:
            media_kwargs['parse_mode'] = 'HTML'
        try:
            return await self.edit_media(InputMediaPhoto(**media_kwargs), **edit_kwargs)
        except TelegramBadRequest as error:
            if is_topic_required_error(error):
                return None
            if is_privacy_restricted_error(error):
                fallback_text = append_privacy_hint(text, language)
                safe_kwargs = prepare_privacy_safe_kwargs(kwargs)
                try:
                    await self.delete()
                except Exception:
                    pass
                try:
                    return await _original_answer(self, fallback_text, **safe_kwargs)
                except TelegramBadRequest as inner_error:
                    if is_topic_required_error(inner_error):
                        return None
                    raise
            # Фоллбек: удалим и отправим обычный текст без фото
            try:
                await self.delete()
            except Exception:
                pass
            try:
                return await _original_answer(self, text, **kwargs)
            except TelegramBadRequest as inner_error:
                if is_topic_required_error(inner_error):
                    return None
                raise
    # Обработка ошибок MESSAGE_ID_INVALID для сообщений без фото
    try:
        return await _original_edit_text(self, text, **kwargs)
    except TelegramBadRequest as error:
        if is_topic_required_error(error):
            return None
        if 'MESSAGE_ID_INVALID' in str(error) or 'message to edit not found' in str(error).lower():
            # Сообщение удалено или недоступно — просто игнорируем
            return None
        raise


def patch_message_methods():
    if not settings.ENABLE_LOGO_MODE:
        return
    Message.answer = _answer_with_photo
    Message.edit_text = _edit_with_photo
