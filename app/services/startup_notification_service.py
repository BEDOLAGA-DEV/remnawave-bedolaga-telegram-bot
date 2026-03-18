"""
Сервис стартового уведомления бота.

Отправляет красивое сообщение с информацией о системе при запуске бота.
"""

import html
from datetime import UTC, datetime
from typing import Final

import structlog
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, SubscriptionStatus, Ticket, TicketStatus, User, UserStatus
from app.external.remnawave_api import RemnaWaveAPI, test_api_connection
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)

# Константы
DEFAULT_VERSION: Final[str] = 'dev'
DEFAULT_AUTH_TYPE: Final[str] = 'api_key'

# Форматирование
KOPEKS_IN_RUBLE: Final[int] = 100
MILLION: Final[int] = 1_000_000
THOUSAND: Final[int] = 1_000
DATETIME_FORMAT: Final[str] = '%d.%m.%Y %H:%M:%S'
DATETIME_FORMAT_FILENAME: Final[str] = '%Y%m%d_%H%M%S'
REPORT_SEPARATOR_WIDTH: Final[int] = 50

# Лимиты сообщений
CRASH_ERROR_MESSAGE_MAX_LENGTH: Final[int] = 1000
CRASH_ERROR_PREVIEW_LENGTH: Final[int] = 200

# URL-ы
GITHUB_BOT_URL: Final[str] = 'https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot'
GITHUB_CABINET_URL: Final[str] = 'https://github.com/BEDOLAGA-DEV/bedolaga-cabinet'
COMMUNITY_URL: Final[str] = 'https://t.me/+wTdMtSWq8YdmZmVi'
DEVELOPER_CONTACT_URL: Final[str] = 'https://t.me/fringg'

# Ключевые слова для определения типа ошибки
WEBHOOK_ERROR_KEYWORDS: Final[tuple[str, ...]] = ('webhook', 'failed to resolve host')
DATABASE_ERROR_KEYWORDS: Final[tuple[str, ...]] = ('database', 'postgres', 'connection refused')
REDIS_ERROR_KEYWORD: Final[str] = 'redis'
REMNAWAVE_ERROR_KEYWORDS: Final[tuple[str, ...]] = ('remnawave', 'panel')
AUTH_ERROR_KEYWORDS: Final[tuple[str, ...]] = ('unauthorized', 'bot token')
INLINE_BUTTON_URL_ERROR_KEYWORDS: Final[tuple[str, ...]] = (
    'web app url',
    'url host is empty',
    'unsupported url protocol',
    'button url',
)


class StartupNotificationService:
    """Сервис для отправки стартового уведомления в админский чат."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.chat_id = getattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', None)
        # Стартовые/краш-уведомления → топик инфраструктуры, fallback на общий
        self.topic_id = getattr(settings, 'ADMIN_NOTIFICATIONS_INFRASTRUCTURE_TOPIC_ID', None) or getattr(
            settings, 'ADMIN_NOTIFICATIONS_TOPIC_ID', None
        )
        self.enabled = getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)

    def _get_version(self) -> str:
        """Получает версию из pyproject.toml."""
        try:
            from pathlib import Path

            pyproject_path = Path(__file__).resolve().parents[2] / 'pyproject.toml'
            if pyproject_path.exists():
                for line in pyproject_path.read_text().splitlines():
                    if line.strip().startswith('version'):
                        ver = line.split('=', 1)[1].strip().strip('"').strip("'")
                        if ver:
                            return ver
        except Exception:
            pass

        return DEFAULT_VERSION

    async def _get_users_count(self) -> int:
        """Получает количество активных пользователей в базе."""
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(func.count(User.id)).where(User.status == UserStatus.ACTIVE.value))
                return result.scalar() or 0
        except Exception as e:
            logger.error('Ошибка получения количества пользователей', e=e)
            return 0

    async def _get_total_balance(self) -> int:
        """Получает сумму балансов всех пользователей в копейках."""
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(func.coalesce(func.sum(User.balance_kopeks), 0)).where(
                        User.status == UserStatus.ACTIVE.value
                    )
                )
                return result.scalar() or 0
        except Exception as e:
            logger.error('Ошибка получения суммы балансов', e=e)
            return 0

    async def _get_open_tickets_count(self) -> int:
        """Получает количество открытых тикетов."""
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(func.count(Ticket.id)).where(Ticket.status == TicketStatus.OPEN.value))
                return result.scalar() or 0
        except Exception as e:
            logger.error('Ошибка получения количества открытых тикетов', e=e)
            return 0

    async def _get_paid_subscriptions_count(self) -> int:
        """Получает количество платных подписок (не триальных, активных)."""
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(func.count(Subscription.id)).where(
                        Subscription.is_trial == False,
                        Subscription.status == SubscriptionStatus.ACTIVE.value,
                    )
                )
                return result.scalar() or 0
        except Exception as e:
            logger.error('Ошибка получения количества платных подписок', e=e)
            return 0

    async def _get_trial_subscriptions_count(self) -> int:
        """Получает количество триальных подписок."""
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(func.count(Subscription.id)).where(Subscription.is_trial == True))
                return result.scalar() or 0
        except Exception as e:
            logger.error('Ошибка получения количества триальных подписок', e=e)
            return 0

    async def _check_remnawave_connection(self) -> tuple[bool, str]:
        """
        Проверяет соединение с панелью Remnawave.

        Returns:
            Tuple[bool, str]: (is_connected, status_message)
        """
        try:
            auth_params = settings.get_remnawave_auth_params()
            base_url = (auth_params.get('base_url') or '').strip()
            api_key = (auth_params.get('api_key') or '').strip()

            if not base_url or not api_key:
                return False, 'Не настроен'

            secret_key = (auth_params.get('secret_key') or '').strip() or None
            username = (auth_params.get('username') or '').strip() or None
            password = (auth_params.get('password') or '').strip() or None
            caddy_token = (auth_params.get('caddy_token') or '').strip() or None
            auth_type = (auth_params.get('auth_type') or DEFAULT_AUTH_TYPE).strip()

            api = RemnaWaveAPI(
                base_url=base_url,
                api_key=api_key,
                secret_key=secret_key,
                username=username,
                password=password,
                caddy_token=caddy_token,
                auth_type=auth_type,
            )

            async with api:
                is_connected = await test_api_connection(api)
                if is_connected:
                    return True, 'Подключено'
                return False, 'Недоступна'

        except Exception as e:
            logger.error('Ошибка проверки соединения с Remnawave', e=e)
            return False, 'Ошибка подключения'

    def _format_balance(self, kopeks: int) -> str:
        """Форматирует баланс в рублях."""
        rubles = kopeks / KOPEKS_IN_RUBLE
        if rubles >= MILLION:
            return f'{rubles / MILLION:.2f}M RUB'
        if rubles >= THOUSAND:
            return f'{rubles / THOUSAND:.1f}K RUB'
        return f'{rubles:.2f} RUB'

    async def send_startup_notification(self) -> bool:
        """
        Отправляет стартовое уведомление в админский чат.

        Returns:
            bool: True если сообщение отправлено успешно
        """
        if not self.enabled or not self.chat_id:
            logger.debug('Стартовое уведомление отключено или chat_id не задан')
            return False

        try:
            version = self._get_version()
            users_count = await self._get_users_count()
            total_balance_kopeks = await self._get_total_balance()
            open_tickets_count = await self._get_open_tickets_count()
            paid_subscriptions_count = await self._get_paid_subscriptions_count()
            trial_subscriptions_count = await self._get_trial_subscriptions_count()
            remnawave_connected, remnawave_status = await self._check_remnawave_connection()

            # Иконка статуса Remnawave
            remnawave_icon = '🟢' if remnawave_connected else '🔴'

            # Формируем системную информацию для blockquote
            system_info_lines = [
                f'Версия: {version}',
                f'Пользователей: {users_count:,}'.replace(',', ' '),
                f'Сумма балансов: {self._format_balance(total_balance_kopeks)}',
                f'Платных подписок: {paid_subscriptions_count:,}'.replace(',', ' '),
                f'Триальных подписок: {trial_subscriptions_count:,}'.replace(',', ' '),
                f'Открытых тикетов: {open_tickets_count:,}'.replace(',', ' '),
                f'{remnawave_icon} Remnawave: {remnawave_status}',
            ]
            system_info = '\n'.join(system_info_lines)

            timestamp = format_local_datetime(datetime.now(UTC), DATETIME_FORMAT)

            message = (
                f'<b>Remnawave Bedolaga Bot</b>\n\n'
                f'✅ Бот успешно запущен\n\n'
                f'<blockquote expandable>{system_info}</blockquote>\n\n'
                f'<i>{timestamp}</i>'
            )

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text='Поставить звезду',
                            url=GITHUB_BOT_URL,
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text='Вебкабинет',
                            url=GITHUB_CABINET_URL,
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text='Сообщество',
                            url=COMMUNITY_URL,
                        ),
                    ],
                ]
            )

            message_kwargs: dict = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': ParseMode.HTML,
                'reply_markup': keyboard,
                'disable_web_page_preview': True,
            }

            if self.topic_id:
                message_kwargs['message_thread_id'] = self.topic_id

            await self.bot.send_message(**message_kwargs)
            logger.info('Стартовое уведомление отправлено в чат', chat_id=self.chat_id)
            return True

        except Exception as e:
            logger.error('Ошибка отправки стартового уведомления', e=e)
            return False


async def send_bot_startup_notification(bot: Bot) -> bool:
    """
    Удобная функция для отправки стартового уведомления.

    Args:
        bot: Экземпляр бота aiogram

    Returns:
        bool: True если уведомление отправлено успешно
    """
    service = StartupNotificationService(bot)
    return await service.send_startup_notification()


def _get_error_recommendations(error_message: str) -> str | None:
    """
    Возвращает рекомендации по исправлению ошибки на основе текста ошибки.

    Args:
        error_message: Текст ошибки

    Returns:
        Рекомендации в формате HTML blockquote или None
    """
    error_lower = error_message.lower()

    # Ошибки вебхука
    if any(keyword in error_lower for keyword in WEBHOOK_ERROR_KEYWORDS):
        tips = [
            '• Проверьте WEBHOOK_HOST в .env',
            '• Убедитесь что домен доступен извне',
            '• Проверьте SSL сертификат (должен быть валидный)',
            '• Проверьте reverse proxy (nginx/caddy)',
            '• Проверьте сеть Docker (docker network)',
            '• Попробуйте: docker compose restart',
        ]
        return '<blockquote expandable>💡 <b>Рекомендации:</b>\n' + '\n'.join(tips) + '</blockquote>'

    # Ошибки подключения к БД
    if any(keyword in error_lower for keyword in DATABASE_ERROR_KEYWORDS):
        tips = [
            '• Проверьте что PostgreSQL запущен',
            '• Проверьте DATABASE_URL в .env',
            '• Проверьте сеть Docker между контейнерами',
            '• Попробуйте: docker compose restart db',
        ]
        return '<blockquote expandable>💡 <b>Рекомендации:</b>\n' + '\n'.join(tips) + '</blockquote>'

    # Ошибки Redis
    if REDIS_ERROR_KEYWORD in error_lower:
        tips = [
            '• Проверьте что Redis запущен',
            '• Проверьте REDIS_URL в .env',
            '• Попробуйте: docker compose restart redis',
        ]
        return '<blockquote expandable>💡 <b>Рекомендации:</b>\n' + '\n'.join(tips) + '</blockquote>'

    # Ошибки Remnawave API
    if any(keyword in error_lower for keyword in REMNAWAVE_ERROR_KEYWORDS):
        tips = [
            '• Проверьте REMNAWAVE_API_URL в .env',
            '• Проверьте REMNAWAVE_API_KEY',
            '• Убедитесь что панель Remnawave доступна',
        ]
        return '<blockquote expandable>💡 <b>Рекомендации:</b>\n' + '\n'.join(tips) + '</blockquote>'

    # Ошибки токена бота
    if any(keyword in error_lower for keyword in AUTH_ERROR_KEYWORDS):
        tips = [
            '• Проверьте BOT_TOKEN в .env',
            '• Убедитесь что токен актуален (@BotFather)',
        ]
        return '<blockquote expandable>💡 <b>Рекомендации:</b>\n' + '\n'.join(tips) + '</blockquote>'

    # Ошибки inline-кнопок с URL (WebApp, кастомные протоколы)
    if any(keyword in error_lower for keyword in INLINE_BUTTON_URL_ERROR_KEYWORDS):
        tips = [
            '• Проверьте MINIAPP_CUSTOM_URL в .env',
            '• Проверьте HAPP_CRYPTOLINK_REDIRECT_TEMPLATE',
            '• Telegram не поддерживает кастомные схемы (happ://, v2ray://, ss://, и т.д.) в inline-кнопках',
            '• Используйте HTTPS редирект для диплинков',
        ]
        return '<blockquote expandable>💡 <b>Рекомендации:</b>\n' + '\n'.join(tips) + '</blockquote>'

    return None


async def send_crash_notification(bot: Bot, error: Exception, traceback_str: str) -> bool:
    """
    Отправляет уведомление о падении бота с лог-файлом.

    Args:
        bot: Экземпляр бота aiogram
        error: Исключение, вызвавшее падение
        traceback_str: Строка с полным traceback

    Returns:
        bool: True если уведомление отправлено успешно
    """
    chat_id = getattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', None)
    # Краш → топик ошибок, fallback на общий
    topic_id = getattr(settings, 'ADMIN_NOTIFICATIONS_ERRORS_TOPIC_ID', None) or getattr(
        settings, 'ADMIN_NOTIFICATIONS_TOPIC_ID', None
    )
    enabled = getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)

    if not enabled or not chat_id:
        logger.debug('Уведомление о падении отключено или chat_id не задан')
        return False

    try:
        timestamp = format_local_datetime(datetime.now(UTC), DATETIME_FORMAT)
        error_type = type(error).__name__
        error_message = str(error)[:CRASH_ERROR_MESSAGE_MAX_LENGTH]
        separator = '=' * REPORT_SEPARATOR_WIDTH

        # Формируем содержимое лог-файла
        log_content = (
            f'CRASH REPORT\n'
            f'{separator}\n\n'
            f'Timestamp: {timestamp}\n'
            f'Error Type: {error_type}\n'
            f'Error Message: {error_message}\n\n'
            f'{separator}\n'
            f'TRACEBACK\n'
            f'{separator}\n\n'
            f'{traceback_str}\n'
        )

        # Создаем файл для отправки
        file_name = f'crash_report_{datetime.now(UTC).strftime(DATETIME_FORMAT_FILENAME)}.txt'
        file = BufferedInputFile(
            file=log_content.encode('utf-8'),
            filename=file_name,
        )

        # Текст сообщения (escape HTML в error_type/message — они могут содержать <class ...>)
        message_text = (
            f'<b>Remnawave Bedolaga Bot</b>\n\n'
            f'❌ Бот упал с ошибкой\n\n'
            f'<b>Тип:</b> <code>{html.escape(error_type)}</code>\n'
            f'<b>Сообщение:</b> <code>{html.escape(error_message[:CRASH_ERROR_PREVIEW_LENGTH])}</code>\n'
        )

        # Добавляем рекомендации если есть
        recommendations = _get_error_recommendations(error_message)
        if recommendations:
            message_text += f'\n{recommendations}\n'

        message_text += f'\n<i>{timestamp}</i>'

        # Кнопка для связи с разработчиком
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
        logger.info('Уведомление о падении отправлено в чат', chat_id=chat_id)
        return True

    except Exception as e:
        logger.error('Ошибка отправки уведомления о падении', e=e)
        return False
