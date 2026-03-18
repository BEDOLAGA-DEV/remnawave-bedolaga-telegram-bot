import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import structlog
from aiogram import BaseMiddleware, Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, TelegramObject
from sqlalchemy.exc import InterfaceError, OperationalError

from app.config import settings
from app.services.startup_notification_service import _get_error_recommendations
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)

# Константы
ERROR_NOTIFICATION_COOLDOWN_MINUTES: Final[int] = 5
ERROR_BUFFER_MAX_SIZE: Final[int] = 10
ERROR_MESSAGE_MAX_LENGTH: Final[int] = 500
REPORT_SEPARATOR_WIDTH: Final[int] = 50
DATETIME_FORMAT: Final[str] = '%d.%m.%Y %H:%M:%S'
DATETIME_FORMAT_FILENAME: Final[str] = '%Y%m%d_%H%M%S'
DEVELOPER_CONTACT_URL: Final[str] = 'https://t.me/fringg'

# Фразы ошибок Telegram API
OLD_QUERY_PHRASES: Final[tuple[str, ...]] = (
    'query is too old',
    'query id is invalid',
    'response timeout expired',
)
BAD_REQUEST_PHRASES: Final[tuple[str, ...]] = (
    'message not found',
    'chat not found',
    'bot was blocked by the user',
    'user is deactivated',
)
TOPIC_ERROR_PHRASES: Final[tuple[str, ...]] = (
    'topic must be specified',
    'topic_closed',
    'topic_deleted',
    'forum_closed',
)
MESSAGE_NOT_MODIFIED_PHRASE: Final[str] = 'message is not modified'
BOT_BLOCKED_PHRASE: Final[str] = 'bot was blocked'
USER_DEACTIVATED_PHRASE: Final[str] = 'user is deactivated'
CHAT_NOT_FOUND_PHRASE: Final[str] = 'chat not found'
MESSAGE_NOT_FOUND_PHRASE: Final[str] = 'message not found'

# Троттлинг для предотвращения спама ошибками
_last_error_notification: datetime | None = None
_error_notification_cooldown = timedelta(minutes=ERROR_NOTIFICATION_COOLDOWN_MINUTES)
_error_buffer: list[tuple[str, str, str]] = []  # (error_type, error_message, traceback)


class GlobalErrorMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except TelegramBadRequest as e:
            return await self._handle_telegram_error(event, e, data)
        except (InterfaceError, OperationalError) as e:
            # Ошибки соединения с БД (таймаут после долгих операций) - логируем, но не спамим админам
            logger.warning('⚠️ Ошибка соединения с БД в GlobalErrorMiddleware', e=e)
            raise
        except Exception as e:
            user_info = self._get_user_info(event)
            logger.error('Неожиданная ошибка в GlobalErrorMiddleware (user=)', user_info=user_info, e=e, exc_info=True)
            raise

    async def _handle_telegram_error(self, event: TelegramObject, error: TelegramBadRequest, data: dict[str, Any]):
        error_message = str(error).lower()

        if self._is_old_query_error(error_message):
            return await self._handle_old_query(event, error)
        if self._is_message_not_modified_error(error_message):
            return await self._handle_message_not_modified(event, error, data)
        if self._is_topic_required_error(error_message):
            # Канал с топиками — просто игнорируем
            logger.debug('[GlobalErrorMiddleware] Игнорируем ошибку топика', error=error)
            return None
        if self._is_bad_request_error(error_message):
            return await self._handle_bad_request(event, error, data)

        # Неизвестная ошибка — логируем
        user_info = self._get_user_info(event)
        logger.error('Неизвестная Telegram API ошибка (user=)', user_info=user_info, error=error)
        raise error

    def _is_old_query_error(self, error_message: str) -> bool:
        return any(phrase in error_message for phrase in OLD_QUERY_PHRASES)

    def _is_message_not_modified_error(self, error_message: str) -> bool:
        return MESSAGE_NOT_MODIFIED_PHRASE in error_message

    def _is_bad_request_error(self, error_message: str) -> bool:
        return any(phrase in error_message for phrase in BAD_REQUEST_PHRASES)

    def _is_topic_required_error(self, error_message: str) -> bool:
        return any(phrase in error_message for phrase in TOPIC_ERROR_PHRASES)

    async def _handle_old_query(self, event: TelegramObject, error: TelegramBadRequest):
        if isinstance(event, CallbackQuery):
            user_info = self._get_user_info(event)
            logger.warning(
                "[GlobalErrorMiddleware] Игнорируем устаревший callback '' от",
                event_data=event.data,
                user_info=user_info,
            )
        else:
            logger.warning('[GlobalErrorMiddleware] Игнорируем устаревший запрос', error=error)

    async def _handle_message_not_modified(
        self, event: TelegramObject, error: TelegramBadRequest, data: dict[str, Any]
    ):
        logger.debug('[GlobalErrorMiddleware] Сообщение не было изменено', error=error)

        if isinstance(event, CallbackQuery):
            try:
                await event.answer()
                logger.debug("Успешно ответили на callback после 'message not modified'")
            except TelegramBadRequest as answer_error:
                if not self._is_old_query_error(str(answer_error).lower()):
                    logger.warning('Ошибка при ответе на callback', answer_error=answer_error)

    async def _handle_bad_request(self, event: TelegramObject, error: TelegramBadRequest, data: dict[str, Any]):
        error_message = str(error).lower()

        if BOT_BLOCKED_PHRASE in error_message:
            user_info = self._get_user_info(event) if hasattr(event, 'from_user') else 'Unknown'
            logger.info('[GlobalErrorMiddleware] Бот заблокирован пользователем', user_info=user_info)
            return
        if USER_DEACTIVATED_PHRASE in error_message:
            user_info = self._get_user_info(event) if hasattr(event, 'from_user') else 'Unknown'
            logger.info('[GlobalErrorMiddleware] Пользователь деактивирован', user_info=user_info)
            return
        if CHAT_NOT_FOUND_PHRASE in error_message or MESSAGE_NOT_FOUND_PHRASE in error_message:
            logger.warning('[GlobalErrorMiddleware] Чат или сообщение не найдено', error=error)
            return
        user_info = self._get_user_info(event)
        logger.error('[GlobalErrorMiddleware] Неизвестная bad request ошибка (user=)', user_info=user_info, error=error)
        raise error

    def _get_user_info(self, event: TelegramObject) -> str:
        if hasattr(event, 'from_user') and event.from_user:
            if event.from_user.username:
                return f'@{event.from_user.username}'
            return f'ID:{event.from_user.id}'
        return 'Unknown'


class ErrorStatisticsMiddleware(BaseMiddleware):
    def __init__(self):
        self.error_counts = {
            'old_queries': 0,
            'message_not_modified': 0,
            'bot_blocked': 0,
            'user_deactivated': 0,
            'other_errors': 0,
        }

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except TelegramBadRequest as e:
            self._count_error(e)
            raise

    def _count_error(self, error: TelegramBadRequest):
        error_message = str(error).lower()

        if OLD_QUERY_PHRASES[0] in error_message:
            self.error_counts['old_queries'] += 1
        elif MESSAGE_NOT_MODIFIED_PHRASE in error_message:
            self.error_counts['message_not_modified'] += 1
        elif BOT_BLOCKED_PHRASE in error_message:
            self.error_counts['bot_blocked'] += 1
        elif USER_DEACTIVATED_PHRASE in error_message:
            self.error_counts['user_deactivated'] += 1
        else:
            self.error_counts['other_errors'] += 1

    def get_statistics(self) -> dict:
        return self.error_counts.copy()

    def reset_statistics(self):
        for key in self.error_counts:
            self.error_counts[key] = 0


async def send_error_to_admin_chat(
    bot: Bot, error: Exception, context: str = '', tb_override: str | None = None
) -> bool:
    """
    Отправляет уведомление об ошибке в админский чат с троттлингом.

    Args:
        bot: Экземпляр бота
        error: Исключение
        context: Дополнительный контекст (например, информация о пользователе)
        tb_override: Готовый traceback (если вызывается не из except-блока)

    Returns:
        bool: True если уведомление отправлено
    """
    global _last_error_notification

    chat_id = getattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', None)
    # Используем топик для ошибок, если настроен, иначе общий
    topic_id = getattr(settings, 'ADMIN_NOTIFICATIONS_ERRORS_TOPIC_ID', None) or getattr(
        settings, 'ADMIN_NOTIFICATIONS_TOPIC_ID', None
    )
    enabled = getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)

    if not enabled or not chat_id:
        return False

    error_type = type(error).__name__
    error_message = str(error)[:ERROR_MESSAGE_MAX_LENGTH]
    tb_str = tb_override or traceback.format_exc()
    if tb_str == 'NoneType: None\n' or tb_str == 'NoneType: None':
        tb_str = '(no traceback available)'

    # Добавляем в буфер
    _error_buffer.append((error_type, error_message, tb_str))
    if len(_error_buffer) > ERROR_BUFFER_MAX_SIZE:
        _error_buffer.pop(0)

    # Проверяем троттлинг
    now = datetime.now(tz=UTC)
    if _last_error_notification and (now - _last_error_notification) < _error_notification_cooldown:
        logger.debug('Ошибка добавлена в буфер, троттлинг активен', error_type=error_type)
        return False

    _last_error_notification = now

    try:
        timestamp = format_local_datetime(now, DATETIME_FORMAT)
        separator = '=' * REPORT_SEPARATOR_WIDTH

        # Формируем лог-файл со всеми ошибками из буфера
        log_lines = [
            'ERROR REPORT',
            separator,
            f'Timestamp: {timestamp}',
            f'Errors in buffer: {len(_error_buffer)}',
            '',
        ]

        for i, (err_type, err_msg, err_tb) in enumerate(_error_buffer):
            log_lines.extend(
                [
                    separator,
                    f'ERROR #{i}: {err_type}',
                    separator,
                    f'Message: {err_msg}',
                    '',
                    'Traceback:',
                    err_tb,
                    '',
                ]
            )

        log_content = '\n'.join(log_lines)

        errors_count = len(_error_buffer)

        file_name = f'error_report_{now.strftime(DATETIME_FORMAT_FILENAME)}.txt'
        file = BufferedInputFile(
            file=log_content.encode('utf-8'),
            filename=file_name,
        )

        message_text = (
            f'<b>Remnawave Bedolaga Bot</b>\n\n'
            f'⚠️ Ошибка во время работы\n\n'
            f'<b>Тип:</b> <code>{error_type}</code>\n'
            f'<b>Ошибок в отчёте:</b> {errors_count}\n'
        )
        if context:
            message_text += f'<b>Контекст:</b> {context}\n'

        # Добавляем рекомендации если есть
        recommendations = _get_error_recommendations(error_message)
        if recommendations:
            message_text += f'\n{recommendations}\n'

        message_text += f'\n<i>{timestamp}</i>'

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text='💬 Сообщить разработчику',
                        url=DEVELOPER_CONTACT_URL,
                    ),
                ],
            ]
        )

        message_kwargs: dict = {
            'chat_id': chat_id,
            'document': file,
            'caption': message_text,
            'parse_mode': ParseMode.HTML,
            'reply_markup': keyboard,
        }

        if topic_id:
            message_kwargs['message_thread_id'] = topic_id

        await bot.send_document(**message_kwargs)
        _error_buffer.clear()  # Clear only after successful send
        logger.info('Уведомление об ошибке отправлено в чат', chat_id=chat_id)
        return True

    except Exception as e:
        logger.error('Ошибка отправки уведомления об ошибке', e=e, _admin_notified=True)
        return False
