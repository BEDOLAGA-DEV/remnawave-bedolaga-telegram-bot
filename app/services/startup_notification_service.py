"""
Сервис стартового уведомления бота.

Отправляет красивое сообщение с информацией о системе при запуске бота.
"""

import asyncio
import html
import signal
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import structlog
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import (
    Subscription,
    SubscriptionStatus,
    Ticket,
    TicketStatus,
    Transaction,
    TransactionType,
    User,
    UserStatus,
)
from app.external.remnawave_api import RemnaWaveAPI, test_api_connection
from app.localization.texts import Texts, get_texts
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)

# Константы
DEFAULT_VERSION: Final[str] = 'dev'
DEFAULT_AUTH_TYPE: Final[str] = 'api_key'

# Форматирование
KOPEKS_IN_RUBLE: Final[int] = 100
DATETIME_FORMAT: Final[str] = '%d.%m.%Y %H:%M:%S'
DATETIME_FORMAT_FILENAME: Final[str] = '%Y%m%d_%H%M%S'
REPORT_SEPARATOR_WIDTH: Final[int] = 50

# Лимиты сообщений
CRASH_ERROR_MESSAGE_MAX_LENGTH: Final[int] = 1000
CRASH_ERROR_PREVIEW_LENGTH: Final[int] = 200

# Окно «за сутки» в сводке запуска
RECENT_WINDOW: Final[timedelta] = timedelta(hours=24)
# Прочерк вместо числа, которое не удалось посчитать: ноль врал бы
NO_VALUE: Final[str] = '—'

# URL-ы
GITHUB_BOT_URL: Final[str] = 'https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot'
GITHUB_CABINET_URL: Final[str] = 'https://github.com/BEDOLAGA-DEV/bedolaga-cabinet'
COMMUNITY_URL: Final[str] = 'https://t.me/+wTdMtSWq8YdmZmVi'
DEVELOPER_CONTACT_URL: Final[str] = 'https://t.me/fringg'

# Ключевые слова для определения типа ошибки
PERMISSION_ERROR_KEYWORDS: Final[tuple[str, ...]] = ('permission denied', 'errno 13')
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


@dataclass(frozen=True, slots=True)
class _StartupStats:
    """Показатели сводки запуска. ``None`` — посчитать не удалось."""

    version: str
    users: int | None
    users_new: int | None
    paid: int | None
    trials: int | None
    deposits_kopeks: int | None
    balances_kopeks: int | None
    open_tickets: int | None
    panel_connected: bool
    panel_status: str
    panel_latency_ms: int | None
    maintenance: bool
    sales_mode: str


async def _recipient_texts(chat_id: int | str | None, topic_id: int | None) -> Texts:
    """Use the configured locale for shared chats; resolve a known private admin once."""
    fallback = get_texts(settings.DEFAULT_LANGUAGE)
    if topic_id:
        return fallback
    try:
        recipient_id = int(chat_id)
        if recipient_id <= 0:
            return fallback
        from app.services.support_settings_service import SupportSettingsService

        if not settings.is_admin(recipient_id) and not SupportSettingsService.is_moderator(recipient_id):
            return fallback
        async with AsyncSessionLocal() as db:
            language = await db.scalar(select(User.language).where(User.telegram_id == recipient_id))
        return get_texts(language) if isinstance(language, str) and language else fallback
    except Exception:
        logger.debug('Startup notification recipient language unavailable; using configured default')
        return fallback


def _number(value: int | None) -> str:
    """4558 → «4 558» (узкий неразрывный пробел — число не рвётся переносом)."""
    return NO_VALUE if value is None else f'{value:,}'.replace(',', '\u202f')


def _rubles(kopeks: int | None) -> str:
    """Рубли целиком: «24.0K RUB» читался хуже, чем «24 012 ₽»."""
    return NO_VALUE if kopeks is None else f'{_number(kopeks // KOPEKS_IN_RUBLE)} ₽'


def _panel_line(stats: _StartupStats, texts: Texts | None = None) -> str:
    texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
    icon = '🟢' if stats.panel_connected else '🔴'
    latency = (
        texts.t('STARTUP_NOTICE_002', ' · {v0} мс').format(v0=stats.panel_latency_ms)
        if stats.panel_latency_ms is not None
        else ''
    )
    return texts.t('STARTUP_NOTICE_000', '{v0} Панель {v1}{v2}').format(
        v0=icon,
        v1=html.escape(
            {
                'не настроена': texts.t('STARTUP_NOTICE_079', 'не настроена'),
                'недоступна': texts.t('STARTUP_NOTICE_080', 'недоступна'),
                'ошибка подключения': texts.t('STARTUP_NOTICE_081', 'ошибка подключения'),
                'на связи': texts.t('STARTUP_NOTICE_082', 'на связи'),
            }.get(stats.panel_status, stats.panel_status)
        ),
        v2=latency,
    )


def _attention(stats: _StartupStats, texts: Texts | None = None) -> list[str]:
    """То, что админу стоит сделать сразу после запуска. Пусто — всё в порядке."""
    texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
    items: list[str] = []
    if not stats.panel_connected:
        items.append(texts.t('STARTUP_NOTICE_019', 'Панель Remnawave не отвечает — подписки не синхронизируются'))
    if stats.maintenance:
        items.append(texts.t('STARTUP_NOTICE_020', 'Включён режим техработ — пользователи видят заглушку'))
    if stats.open_tickets:
        items.append(
            texts.t('STARTUP_NOTICE_021', 'Открытых тикетов ждут ответа: {v0}').format(v0=_number(stats.open_tickets))
        )
    return items


def _sections(stats: _StartupStats, texts: Texts | None = None) -> list[tuple[str, list[tuple[str, str]]]]:
    """Разделы сводки: общий источник для классического и rich-вида."""
    texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
    new_users = NO_VALUE if stats.users_new is None else f'+{_number(stats.users_new)}'
    return [
        (
            texts.t('STARTUP_NOTICE_022', '👥 Пользователи'),
            [
                (texts.t('STARTUP_NOTICE_069', 'Всего'), _number(stats.users)),
                (texts.t('STARTUP_NOTICE_070', 'Новых за сутки'), new_users),
            ],
        ),
        (
            texts.t('STARTUP_NOTICE_023', '💳 Активные подписки'),
            [
                (texts.t('STARTUP_NOTICE_071', 'Платных'), _number(stats.paid)),
                (texts.t('STARTUP_NOTICE_072', 'Триалов'), _number(stats.trials)),
            ],
        ),
        (
            texts.t('STARTUP_NOTICE_024', '💰 Деньги'),
            [
                (texts.t('STARTUP_NOTICE_073', 'Пополнения за сутки'), _rubles(stats.deposits_kopeks)),
                (texts.t('STARTUP_NOTICE_074', 'На балансах'), _rubles(stats.balances_kopeks)),
            ],
        ),
    ]


def _mode_label(stats: _StartupStats, texts: Texts | None = None) -> str:
    texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
    return {
        'classic': texts.t('STARTUP_NOTICE_064', 'классика'),
        'tariffs': texts.t('STARTUP_NOTICE_065', 'тарифы'),
        'multi_tariff': texts.t('STARTUP_NOTICE_066', 'мультитариф'),
    }.get(stats.sales_mode, stats.sales_mode)


def render_startup_message(stats: _StartupStats, *, timestamp: str, texts: Texts | None = None) -> str:
    """Классический вид (HTML): разделы деревом, внимание — отдельным блоком."""
    texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
    lines = [
        f'🚀 <b>Remnawave Bedolaga Bot</b> · <code>v{html.escape(stats.version)}</code>',
        texts.t('STARTUP_NOTICE_003', '<i>Запущен и готов к работе</i>'),
    ]
    for title, rows in _sections(stats, texts=texts):
        lines.append('')
        lines.append(f'<b>{title}</b>')
        for index, (label, value) in enumerate(rows):
            branch = '└' if index == len(rows) - 1 else '├'
            lines.append(f'{branch} {label}: <b>{value}</b>')

    lines += [
        '',
        texts.t('STARTUP_NOTICE_004', '<b>🛠 Система</b>'),
        f'├ {_panel_line(stats, texts=texts)}',
        texts.t('STARTUP_NOTICE_005', '├ Тикетов открыто: <b>{v0}</b>').format(v0=_number(stats.open_tickets)),
        texts.t('STARTUP_NOTICE_006', '└ Режим продаж: {v0}').format(v0=_mode_label(stats, texts=texts)),
    ]

    attention = _attention(stats, texts=texts)
    if attention:
        lines += [
            '',
            texts.t('STARTUP_NOTICE_025', '<blockquote>⚠️ <b>Требует внимания</b>'),
            *(f'• {item}' for item in attention),
        ]
        lines[-1] += '</blockquote>'

    lines += ['', f'<i>{html.escape(timestamp)}</i>']
    return '\n'.join(lines)


def render_startup_rich(stats: _StartupStats, texts: Texts | None = None) -> str:
    """Rich-вид (Bot API 10.1): те же разделы таблицами."""
    texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
    from app.utils.rich_admin import rich_footer_now, rich_kv_table
    from app.utils.rich_menu import _resolve_rich_logo_url

    blocks: list[str] = []
    logo_url = _resolve_rich_logo_url()
    if logo_url:
        blocks.append(f'<img src="{html.escape(logo_url, quote=True)}"/>')
    blocks += [
        f'<h5>🚀 Remnawave Bedolaga Bot · v{html.escape(stats.version)}</h5>',
        texts.t('STARTUP_NOTICE_007', '<p>Запущен и готов к работе</p>'),
    ]
    attention = _attention(stats, texts=texts)
    if attention:
        blocks.append(
            texts.t('STARTUP_NOTICE_075', '<blockquote>⚠️ <b>Требует внимания</b><br>')
            + '<br>'.join(f'• {item}' for item in attention)
            + '</blockquote>'
        )
    for title, rows in _sections(stats, texts=texts):
        blocks.append(f'<p><b>{title}</b></p>')
        blocks.append(rich_kv_table([(label, f'<b>{value}</b>') for label, value in rows]))
    blocks.append(texts.t('STARTUP_NOTICE_008', '<p><b>🛠 Система</b></p>'))
    blocks.append(
        rich_kv_table(
            [
                (texts.t('STARTUP_NOTICE_076', 'Панель'), _panel_line(stats, texts=texts)),
                (texts.t('STARTUP_NOTICE_077', 'Тикетов открыто'), _number(stats.open_tickets)),
                (texts.t('STARTUP_NOTICE_078', 'Режим продаж'), _mode_label(stats, texts=texts)),
            ]
        )
    )
    blocks += ['<hr/>', rich_footer_now()]
    return ''.join(blocks)


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

    async def _count(self, label: str, statement) -> int | None:
        """Одно число из базы; ``None`` — посчитать не удалось (показываем прочерк)."""
        try:
            async with AsyncSessionLocal() as db:
                return int((await db.execute(statement)).scalar() or 0)
        except Exception as e:
            logger.error('Ошибка подсчёта показателя стартового уведомления', metric=label, e=e)
            return None

    async def _collect_stats(self) -> _StartupStats:
        """Все показатели сводки — параллельно, каждый в своей сессии: сбой одного
        запроса не должен обнулить остальные."""
        now = datetime.now(UTC)
        since = now - RECENT_WINDOW
        alive = (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value)
        queries = {
            'users': select(func.count(User.id)).where(User.status == UserStatus.ACTIVE.value),
            'users_new': select(func.count(User.id)).where(
                User.status == UserStatus.ACTIVE.value, User.created_at >= since
            ),
            # Живые — по статусу И по сроку: статус истёкшей подписки гасит планировщик
            # не сразу. Раньше триалы считались все за всю историю, включая истёкшие.
            'paid': select(func.count(Subscription.id)).where(
                Subscription.is_trial.is_(False),
                Subscription.status.in_(alive),
                Subscription.end_date > now,
            ),
            'trials': select(func.count(Subscription.id)).where(
                Subscription.is_trial.is_(True),
                Subscription.status.in_(alive),
                Subscription.end_date > now,
            ),
            'deposits': select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
                Transaction.type == TransactionType.DEPOSIT.value,
                Transaction.is_completed.is_(True),
                Transaction.created_at >= since,
            ),
            'balances': select(func.coalesce(func.sum(User.balance_kopeks), 0)).where(
                User.status == UserStatus.ACTIVE.value
            ),
            'tickets': select(func.count(Ticket.id)).where(Ticket.status == TicketStatus.OPEN.value),
        }
        values, panel = await asyncio.gather(
            asyncio.gather(*(self._count(label, statement) for label, statement in queries.items())),
            self._check_remnawave_connection(),
        )
        counts = dict(zip(queries, values, strict=True))
        connected, status_text, latency_ms = panel
        return _StartupStats(
            version=self._get_version(),
            users=counts['users'],
            users_new=counts['users_new'],
            paid=counts['paid'],
            trials=counts['trials'],
            deposits_kopeks=counts['deposits'],
            balances_kopeks=counts['balances'],
            open_tickets=counts['tickets'],
            panel_connected=connected,
            panel_status=status_text,
            panel_latency_ms=latency_ms,
            maintenance=bool(settings.is_maintenance_mode()),
            # Мультитариф — разновидность режима «тарифы», поэтому одна строка на оба.
            sales_mode='multi_tariff' if settings.is_multi_tariff_enabled() else settings.get_sales_mode(),
        )

    async def _check_remnawave_connection(self) -> tuple[bool, str, int | None]:
        """
        Проверяет соединение с панелью Remnawave.

        Returns:
            (is_connected, status_message, latency_ms) — задержка только у успешной проверки.
        """
        try:
            auth_params = settings.get_remnawave_auth_params()
            base_url = (auth_params.get('base_url') or '').strip()
            api_key = (auth_params.get('api_key') or '').strip()

            if not base_url or not api_key:
                return False, 'не настроена', None

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
                started = time.monotonic()
                is_connected = await test_api_connection(api)
                latency_ms = round((time.monotonic() - started) * 1000)
                if is_connected:
                    return True, 'на связи', latency_ms
                return False, 'недоступна', None

        except Exception as e:
            logger.error('Ошибка проверки соединения с Remnawave', e=e)
            return False, 'ошибка подключения', None

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
            texts = await _recipient_texts(self.chat_id, self.topic_id)
            stats = await self._collect_stats()
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('STARTUP_NOTICE_084', '⭐ Звезда на GitHub'), url=GITHUB_BOT_URL
                        ),
                        InlineKeyboardButton(text=texts.t('STARTUP_NOTICE_085', '🖥 Кабинет'), url=GITHUB_CABINET_URL),
                    ],
                    [InlineKeyboardButton(text=texts.t('STARTUP_NOTICE_086', '💬 Сообщество'), url=COMMUNITY_URL)],
                ]
            )

            # Rich-вид (Bot API 10.1): логотип, заголовок, таблица показателей,
            # footer с tg-time. При недоступности — классический вид ниже.
            try:
                from app.utils.rich_admin import try_send_rich_admin_message

                if await try_send_rich_admin_message(
                    self.bot,
                    self.chat_id,
                    render_startup_rich(stats, texts=texts),
                    thread_id=self.topic_id,
                    reply_markup=keyboard,
                ):
                    logger.info('Rich-стартовое уведомление отправлено в чат', chat_id=self.chat_id)
                    return True
            except Exception as rich_error:
                logger.warning('Сбой rich-рендера стартового уведомления', error=str(rich_error))

            message_kwargs: dict = {
                'chat_id': self.chat_id,
                'text': render_startup_message(
                    stats, timestamp=format_local_datetime(datetime.now(UTC), DATETIME_FORMAT), texts=texts
                ),
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

    async def prewarm_logo(self) -> bool:
        """Заранее загрузить логотип один раз и закешировать его Telegram file_id.

        Без прогрева ~700КБ файл логотипа перезаливается на ПЕРВУЮ отправку каждой
        рассылки/уведомления (file_id кешируется только после первого успеха), что на
        медленном канале до Telegram подвешивает хвост цикла мониторинга. Шлём фото в
        админ-чат (или в ЛС первого админа), ловим file_id и сразу удаляем сообщение.
        Полностью best-effort: на любой ошибке тихо выходим, старт не блокируем.
        """
        try:
            from app.utils.message_patch import _cache_logo_file_id, _logo_file_id, get_logo_media

            if _logo_file_id:
                return True

            media = get_logo_media()
            # None → логотип невалиден/отсутствует; str → file_id уже закеширован.
            if media is None or isinstance(media, str):
                return media is not None

            target = self.chat_id or next(iter(settings.get_admin_ids()), None)
            if not target:
                logger.debug('prewarm_logo: нет целевого чата (admin), пропуск')
                return False

            send_kwargs: dict = {'chat_id': target, 'photo': media, 'disable_notification': True}
            if self.topic_id and target == self.chat_id:
                send_kwargs['message_thread_id'] = self.topic_id

            timeout = getattr(settings, 'MONITORING_NOTIFICATION_SEND_TIMEOUT', 20.0)
            msg = await asyncio.wait_for(self.bot.send_photo(**send_kwargs), timeout=timeout)
            _cache_logo_file_id(msg)

            try:
                await self.bot.delete_message(chat_id=target, message_id=msg.message_id)
            except Exception:
                pass  # удаление best-effort — file_id уже пойман

            logger.info('Логотип прогрет на старте: file_id закеширован', chat_id=target)
            return True
        except Exception as e:
            logger.warning('Не удалось прогреть логотип на старте', error=str(e)[:200])
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
    # Прогреваем file_id логотипа до первых рассылок, чтобы ~700КБ файл не
    # перезаливался на каждой первой отправке (см. баг зависания мониторинга).
    await service.prewarm_logo()
    return await service.send_startup_notification()


def _get_error_recommendations(error_message: str, texts: Texts | None = None) -> str | None:
    """
    Возвращает рекомендации по исправлению ошибки на основе текста ошибки.

    Args:
        error_message: Текст ошибки

    Returns:
        Рекомендации в формате HTML blockquote или None
    """
    texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
    error_lower = error_message.lower()

    # Ошибки прав доступа к примонтированным каталогам (logs/data/locales/uploads)
    if any(keyword in error_lower for keyword in PERMISSION_ERROR_KEYWORDS):
        tips = [
            texts.t('STARTUP_NOTICE_026', '• Бот в контейнере работает от пользователя с uid 1000'),
            texts.t('STARTUP_NOTICE_027', '• Похоже, примонтированные каталоги принадлежат другому пользователю'),
            texts.t('STARTUP_NOTICE_028', '• Проверьте права на каталоги logs, data, locales (и uploads)'),
            texts.t(
                'STARTUP_NOTICE_029', '• Обычно лечится на хосте: <code>chown -R 1000:1000 logs data locales</code>'
            ),
            texts.t('STARTUP_NOTICE_030', '• После исправления: docker compose restart bot'),
        ]
        return (
            texts.t('STARTUP_NOTICE_067', '<blockquote expandable>💡 <b>Рекомендации:</b>\n')
            + '\n'.join(tips)
            + '</blockquote>'
        )

    # Ошибки вебхука
    if any(keyword in error_lower for keyword in WEBHOOK_ERROR_KEYWORDS):
        tips = [
            texts.t('STARTUP_NOTICE_031', '• Проверьте WEBHOOK_HOST в .env'),
            texts.t('STARTUP_NOTICE_032', '• Убедитесь что домен доступен извне'),
            texts.t('STARTUP_NOTICE_033', '• Проверьте SSL сертификат (должен быть валидный)'),
            texts.t('STARTUP_NOTICE_034', '• Проверьте reverse proxy (nginx/caddy)'),
            texts.t('STARTUP_NOTICE_035', '• Проверьте сеть Docker (docker network)'),
            texts.t('STARTUP_NOTICE_036', '• Попробуйте: docker compose restart'),
        ]
        return (
            texts.t('STARTUP_NOTICE_067', '<blockquote expandable>💡 <b>Рекомендации:</b>\n')
            + '\n'.join(tips)
            + '</blockquote>'
        )

    # Ошибки подключения к БД
    if any(keyword in error_lower for keyword in DATABASE_ERROR_KEYWORDS):
        tips = [
            texts.t('STARTUP_NOTICE_037', '• Проверьте что PostgreSQL запущен'),
            texts.t('STARTUP_NOTICE_038', '• Проверьте DATABASE_URL в .env'),
            texts.t('STARTUP_NOTICE_039', '• Проверьте сеть Docker между контейнерами'),
            texts.t('STARTUP_NOTICE_040', '• Попробуйте: docker compose restart db'),
        ]
        return (
            texts.t('STARTUP_NOTICE_067', '<blockquote expandable>💡 <b>Рекомендации:</b>\n')
            + '\n'.join(tips)
            + '</blockquote>'
        )

    # Ошибки Redis
    if REDIS_ERROR_KEYWORD in error_lower:
        tips = [
            texts.t('STARTUP_NOTICE_041', '• Проверьте что Redis запущен'),
            texts.t('STARTUP_NOTICE_042', '• Проверьте REDIS_URL в .env'),
            texts.t('STARTUP_NOTICE_043', '• Попробуйте: docker compose restart redis'),
        ]
        return (
            texts.t('STARTUP_NOTICE_067', '<blockquote expandable>💡 <b>Рекомендации:</b>\n')
            + '\n'.join(tips)
            + '</blockquote>'
        )

    # Ошибки Remnawave API
    if any(keyword in error_lower for keyword in REMNAWAVE_ERROR_KEYWORDS):
        tips = [
            texts.t('STARTUP_NOTICE_044', '• Проверьте REMNAWAVE_API_URL в .env'),
            texts.t('STARTUP_NOTICE_045', '• Проверьте REMNAWAVE_API_KEY'),
            texts.t('STARTUP_NOTICE_046', '• Убедитесь что панель Remnawave доступна'),
        ]
        return (
            texts.t('STARTUP_NOTICE_067', '<blockquote expandable>💡 <b>Рекомендации:</b>\n')
            + '\n'.join(tips)
            + '</blockquote>'
        )

    # Ошибки токена бота
    if any(keyword in error_lower for keyword in AUTH_ERROR_KEYWORDS):
        tips = [
            texts.t('STARTUP_NOTICE_047', '• Проверьте BOT_TOKEN в .env'),
            texts.t('STARTUP_NOTICE_048', '• Убедитесь что токен актуален (@BotFather)'),
        ]
        return (
            texts.t('STARTUP_NOTICE_067', '<blockquote expandable>💡 <b>Рекомендации:</b>\n')
            + '\n'.join(tips)
            + '</blockquote>'
        )

    # Ошибки inline-кнопок с URL (WebApp, кастомные протоколы)
    if any(keyword in error_lower for keyword in INLINE_BUTTON_URL_ERROR_KEYWORDS):
        tips = [
            texts.t('STARTUP_NOTICE_049', '• Проверьте MINIAPP_CUSTOM_URL в .env'),
            texts.t('STARTUP_NOTICE_050', '• Проверьте HAPP_CRYPTOLINK_REDIRECT_TEMPLATE'),
            texts.t(
                'STARTUP_NOTICE_051',
                '• Telegram не поддерживает кастомные схемы (happ://, v2ray://, ss://, и т.д.) в inline-кнопках',
            ),
            texts.t('STARTUP_NOTICE_052', '• Используйте HTTPS редирект для диплинков'),
        ]
        return (
            texts.t('STARTUP_NOTICE_067', '<blockquote expandable>💡 <b>Рекомендации:</b>\n')
            + '\n'.join(tips)
            + '</blockquote>'
        )

    return None


@dataclass(frozen=True, slots=True)
class ShutdownReason:
    """Почему бот останавливается. Сигнал — плановая остановка, ошибка — аварийная."""

    signum: int | None = None
    error: BaseException | None = None
    #: Где случилась ошибка: 'polling' | 'main_loop'.
    source: str | None = None

    @property
    def is_failure(self) -> bool:
        return self.error is not None


# Что означает сигнал для админа: подпись и подсказка, откуда он обычно приходит.
_SIGNAL_REASONS: Final[dict[int, tuple[str, str]]] = {
    signal.SIGTERM.value: (
        'сигнал SIGTERM',
        (
            'Так бота останавливает Docker: <code>docker compose stop</code> / <code>restart</code>, '
            'обновление образа, перезагрузка сервера.'
        ),
    ),
    signal.SIGINT.value: ('сигнал SIGINT (Ctrl+C)', 'Бота остановили из консоли.'),
}
_FAILURE_SOURCES: Final[dict[str, str]] = {
    'polling': 'Telegram polling',
    'main_loop': 'основной цикл',
}
SHUTDOWN_ERROR_PREVIEW_LENGTH: Final[int] = 300


def _signal_label(signum: int | None, texts: Texts | None = None) -> tuple[str, str | None]:
    texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
    if signum is None:
        return texts.t('STARTUP_NOTICE_053', 'без сигнала'), None
    if signum in _SIGNAL_REASONS:
        return {
            signal.SIGTERM.value: (
                texts.t('STARTUP_NOTICE_009', _SIGNAL_REASONS[signal.SIGTERM.value][0]),
                texts.t('STARTUP_NOTICE_010', _SIGNAL_REASONS[signal.SIGTERM.value][1]),
            ),
            signal.SIGINT.value: (
                texts.t('STARTUP_NOTICE_011', _SIGNAL_REASONS[signal.SIGINT.value][0]),
                texts.t('STARTUP_NOTICE_012', _SIGNAL_REASONS[signal.SIGINT.value][1]),
            ),
        }[signum]
    try:
        return texts.t('STARTUP_NOTICE_054', 'сигнал {v0}').format(v0=signal.Signals(signum).name), None
    except ValueError:
        return texts.t('STARTUP_NOTICE_054', 'сигнал {v0}').format(v0=signum), None


def format_uptime(delta: timedelta, texts: Texts | None = None) -> str:
    """«3 д 4 ч 12 мин»; нули в начале опускаются, меньше минуты — словами."""
    texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes < 1:
        return texts.t('STARTUP_NOTICE_013', 'меньше минуты')
    days, rest = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    parts = [
        texts.t('STARTUP_NOTICE_055', '{v0} д').format(v0=days) if days else '',
        texts.t('STARTUP_NOTICE_056', '{v0} ч').format(v0=hours) if hours else '',
        texts.t('STARTUP_NOTICE_057', '{v0} мин').format(v0=minutes) if minutes else '',
    ]
    return ' '.join(part for part in parts if part)


def render_shutdown_message(
    reason: ShutdownReason,
    *,
    version: str,
    started_at: datetime | None,
    now: datetime,
    texts: Texts | None = None,
) -> str:
    """Классический HTML: причина, аптайм, что делать дальше."""
    texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
    header_icon, header_text = (
        ('🔴', texts.t('STARTUP_NOTICE_058', 'Бот остановился из-за ошибки'))
        if reason.is_failure
        else ('🛑', texts.t('STARTUP_NOTICE_059', 'Бот остановлен'))
    )
    lines = [
        f'{header_icon} <b>Remnawave Bedolaga Bot</b> · <code>v{html.escape(version)}</code>',
        f'<i>{header_text}</i>',
        '',
    ]

    hint: str | None
    if reason.is_failure:
        error = reason.error
        source = {**_FAILURE_SOURCES, 'main_loop': texts.t('STARTUP_NOTICE_001', 'основной цикл')}.get(
            reason.source or '', reason.source or texts.t('STARTUP_NOTICE_068', 'неизвестно')
        )
        error_text = f'{type(error).__name__}: {error}'[:SHUTDOWN_ERROR_PREVIEW_LENGTH]
        lines += [
            texts.t('STARTUP_NOTICE_060', '<b>Причина:</b> ошибка — {v0}').format(v0=html.escape(source)),
            f'<code>{html.escape(error_text)}</code>',
        ]
        hint = _get_error_recommendations(str(error), texts=texts)
    else:
        label, hint_text = _signal_label(reason.signum, texts=texts)
        lines.append(
            texts.t('STARTUP_NOTICE_061', '<b>Причина:</b> плановая остановка, {v0}').format(v0=html.escape(label))
        )
        hint = f'<i>{hint_text}</i>' if hint_text else None

    lines.append('')
    if started_at is not None:
        lines.append(
            texts.t('STARTUP_NOTICE_062', '⏱ Проработал: <b>{v0}</b>').format(
                v0=format_uptime(now - started_at, texts=texts)
            )
        )
        lines.append(
            texts.t('STARTUP_NOTICE_063', '🕐 Запущен: {v0}').format(
                v0=html.escape(format_local_datetime(started_at, DATETIME_FORMAT))
            )
        )
    lines.append(
        texts.t('STARTUP_NOTICE_014', '🕓 Остановлен: {v0}').format(
            v0=html.escape(format_local_datetime(now, DATETIME_FORMAT))
        )
    )

    if hint:
        lines += ['', hint]

    next_step = (
        texts.t(
            'STARTUP_NOTICE_015',
            'Docker с политикой <code>restart</code> поднимет бота заново — дождитесь сообщения о запуске.',
        )
        if reason.is_failure
        else texts.t('STARTUP_NOTICE_016', 'Если это перезапуск — дождитесь сообщения о запуске.')
    )
    lines += [
        '',
        texts.t(
            'STARTUP_NOTICE_017',
            '<blockquote>{v0} Не пришло за пару минут — бот не поднялся, проверьте логи контейнера.</blockquote>',
        ).format(v0=next_step),
    ]
    return '\n'.join(lines)


async def send_shutdown_notification(
    bot: Bot,
    reason: ShutdownReason,
    *,
    started_at: datetime | None,
) -> bool:
    """Сообщить в админ-чат, что бот останавливается и почему.

    Шлётся в начале завершения, пока сессия бота жива: Docker даёт на остановку
    около 10 секунд, поэтому вызывающий ограничивает её по времени.
    """
    chat_id = getattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', None)
    if not getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False) or not chat_id:
        return False
    # Плановая остановка — в топик инфраструктуры, как сообщение о запуске; авария — в ошибки.
    topic_key = (
        'ADMIN_NOTIFICATIONS_ERRORS_TOPIC_ID' if reason.is_failure else 'ADMIN_NOTIFICATIONS_INFRASTRUCTURE_TOPIC_ID'
    )
    topic_id = getattr(settings, topic_key, None) or getattr(settings, 'ADMIN_NOTIFICATIONS_TOPIC_ID', None)

    texts = await _recipient_texts(chat_id, topic_id)
    message_kwargs: dict = {
        'chat_id': chat_id,
        'text': render_shutdown_message(
            reason,
            version=StartupNotificationService(bot)._get_version(),
            started_at=started_at,
            now=datetime.now(UTC),
            texts=texts,
        ),
        'parse_mode': ParseMode.HTML,
        'disable_web_page_preview': True,
    }
    if reason.is_failure:
        message_kwargs['reply_markup'] = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('STARTUP_NOTICE_083', '💬 Сообщить разработчику'), url=DEVELOPER_CONTACT_URL
                    )
                ]
            ]
        )
    if topic_id:
        message_kwargs['message_thread_id'] = topic_id

    try:
        await bot.send_message(**message_kwargs)
    except Exception as e:
        logger.error('Ошибка отправки уведомления об остановке', e=e)
        return False
    logger.info('Уведомление об остановке отправлено в чат', chat_id=chat_id, failure=reason.is_failure)
    return True


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
        texts = await _recipient_texts(chat_id, topic_id)
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
        message_text = texts.t(
            'STARTUP_NOTICE_018',
            '<b>Remnawave Bedolaga Bot</b>\n\n❌ Бот упал с ошибкой\n\n<b>Тип:</b> <code>{v0}</code>\n<b>Сообщение:</b> <code>{v1}</code>\n',
        ).format(v0=html.escape(error_type), v1=html.escape(error_message[:CRASH_ERROR_PREVIEW_LENGTH]))

        # Добавляем рекомендации если есть
        recommendations = _get_error_recommendations(error_message, texts=texts)
        if recommendations:
            message_text += f'\n{recommendations}\n'

        message_text += f'\n<i>{timestamp}</i>'

        # Кнопка для связи с разработчиком
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('STARTUP_NOTICE_083', '💬 Сообщить разработчику'),
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
