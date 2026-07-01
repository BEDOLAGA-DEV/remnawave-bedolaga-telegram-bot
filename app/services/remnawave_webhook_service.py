"""
Service for processing incoming RemnaWave backend webhooks.

Handles all webhook scopes: user, user_hwid_devices, node, service, crm.
User events update subscription state and notify the user.
Admin events (node, service, crm) send alerts to the admin notification chat.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import UTC, datetime
from typing import Any

import structlog
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import delete, inspect as sa_inspect
from sqlalchemy.exc import PendingRollbackError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.config import settings
from app.database.crud.subscription import (
    deactivate_subscription,
    decrement_subscription_server_counts,
    expire_subscription,
    get_subscription_by_user_id,
    is_recently_updated_by_webhook,
    reactivate_subscription,
    update_subscription_usage,
)
from app.database.crud.user import get_user_by_id, get_user_by_remnawave_uuid, get_user_by_telegram_id
from app.database.models import Subscription, SubscriptionServer, SubscriptionStatus, User
from app.localization.texts import get_texts
from app.services.admin_notification_service import AdminNotificationService
from app.services.notification_delivery_service import NotificationType, notification_delivery_service
from app.utils.miniapp_buttons import build_miniapp_or_callback_button


logger = structlog.get_logger(__name__)


# Mapping from locale text_key to NotificationType for unified delivery
_TEXT_KEY_TO_NOTIFICATION_TYPE: dict[str, NotificationType] = {
    'WEBHOOK_SUB_EXPIRED': NotificationType.WEBHOOK_SUB_EXPIRED,
    'WEBHOOK_SUB_DISABLED': NotificationType.WEBHOOK_SUB_DISABLED,
    'WEBHOOK_SUB_ENABLED': NotificationType.WEBHOOK_SUB_ENABLED,
    'WEBHOOK_SUB_LIMITED': NotificationType.WEBHOOK_SUB_LIMITED,
    'WEBHOOK_SUB_TRAFFIC_RESET': NotificationType.WEBHOOK_SUB_TRAFFIC_RESET,
    'WEBHOOK_SUB_DELETED': NotificationType.WEBHOOK_SUB_DELETED,
    'WEBHOOK_SUB_REVOKED': NotificationType.WEBHOOK_SUB_REVOKED,
    'WEBHOOK_SUB_EXPIRES_72H': NotificationType.WEBHOOK_SUB_EXPIRING,
    'WEBHOOK_SUB_EXPIRES_48H': NotificationType.WEBHOOK_SUB_EXPIRING,
    'WEBHOOK_SUB_EXPIRES_24H': NotificationType.WEBHOOK_SUB_EXPIRING,
    'WEBHOOK_SUB_EXPIRED_24H_AGO': NotificationType.WEBHOOK_SUB_EXPIRED,
    'WEBHOOK_SUB_FIRST_CONNECTED': NotificationType.WEBHOOK_SUB_FIRST_CONNECTED,
    'WEBHOOK_SUB_BANDWIDTH_THRESHOLD': NotificationType.WEBHOOK_SUB_BANDWIDTH_THRESHOLD,
    'WEBHOOK_USER_NOT_CONNECTED': NotificationType.WEBHOOK_USER_NOT_CONNECTED,
    'WEBHOOK_DEVICE_ADDED': NotificationType.WEBHOOK_DEVICE_ADDED,
    'WEBHOOK_DEVICE_DELETED': NotificationType.WEBHOOK_DEVICE_DELETED,
    'WEBHOOK_TORRENT_DETECTED': NotificationType.WEBHOOK_TORRENT_DETECTED,
}

# Mapping from locale text_key to the Settings toggle that controls it
_TEXT_KEY_TO_SETTING: dict[str, str] = {
    'WEBHOOK_SUB_EXPIRED': 'WEBHOOK_NOTIFY_SUB_EXPIRED',
    'WEBHOOK_SUB_DISABLED': 'WEBHOOK_NOTIFY_SUB_STATUS',
    'WEBHOOK_SUB_ENABLED': 'WEBHOOK_NOTIFY_SUB_STATUS',
    'WEBHOOK_SUB_LIMITED': 'WEBHOOK_NOTIFY_SUB_LIMITED',
    'WEBHOOK_SUB_TRAFFIC_RESET': 'WEBHOOK_NOTIFY_TRAFFIC_RESET',
    'WEBHOOK_SUB_DELETED': 'WEBHOOK_NOTIFY_SUB_DELETED',
    'WEBHOOK_SUB_REVOKED': 'WEBHOOK_NOTIFY_SUB_REVOKED',
    'WEBHOOK_SUB_EXPIRES_72H': 'WEBHOOK_NOTIFY_SUB_EXPIRING',
    'WEBHOOK_SUB_EXPIRES_48H': 'WEBHOOK_NOTIFY_SUB_EXPIRING',
    'WEBHOOK_SUB_EXPIRES_24H': 'WEBHOOK_NOTIFY_SUB_EXPIRING',
    'WEBHOOK_SUB_EXPIRED_24H_AGO': 'WEBHOOK_NOTIFY_SUB_EXPIRED',
    'WEBHOOK_SUB_FIRST_CONNECTED': 'WEBHOOK_NOTIFY_FIRST_CONNECTED',
    'WEBHOOK_SUB_BANDWIDTH_THRESHOLD': 'WEBHOOK_NOTIFY_BANDWIDTH_THRESHOLD',
    'WEBHOOK_USER_NOT_CONNECTED': 'WEBHOOK_NOTIFY_NOT_CONNECTED',
    'WEBHOOK_DEVICE_ADDED': 'WEBHOOK_NOTIFY_DEVICES',
    'WEBHOOK_DEVICE_DELETED': 'WEBHOOK_NOTIFY_DEVICES',
    'WEBHOOK_TORRENT_DETECTED': 'WEBHOOK_NOTIFY_TORRENT_DETECTED',
}

# Remnawave 2.8.0 объединил 4 события об истечении (user.expires_in_72_hours,
# _48_hours, _24_hours, user.expired_24_hours_ago) в одно user.expiration с
# meta.expiration — знаковым числом часов относительно истечения (отрицательное =
# за |N| ч ДО, положительное = через N ч ПОСЛЕ). Канонический конфиг панели
# EXPIRATION_NOTIFICATIONS=[-72, -48, -24, 24] повторяет прежнее поведение.
_EXPIRATION_HOURS_TO_TEXT_KEY: dict[int, str] = {
    -72: 'WEBHOOK_SUB_EXPIRES_72H',
    -48: 'WEBHOOK_SUB_EXPIRES_48H',
    -24: 'WEBHOOK_SUB_EXPIRES_24H',
    24: 'WEBHOOK_SUB_EXPIRED_24H_AGO',
}

# Admin event display names for notification messages
_ADMIN_NODE_EVENTS: dict[str, str] = {
    'node.created': '🟢 Нода создана',
    'node.modified': '🔧 Нода изменена',
    'node.disabled': '🔴 Нода отключена',
    'node.enabled': '🟢 Нода включена',
    'node.deleted': '🗑️ Нода удалена',
    'node.connection_lost': '🚨 Потеряно соединение с нодой',
    'node.connection_restored': '✅ Соединение с нодой восстановлено',
    'node.traffic_notify': '📊 Уведомление о трафике ноды',
}

_ADMIN_SERVICE_EVENTS: dict[str, str] = {
    'service.panel_started': '🚀 Панель RemnaWave запущена',
    'service.login_attempt_failed': '🔐 Неудачная попытка входа в панель',
    'service.login_attempt_success': '🔓 Успешный вход в панель',
    'service.subpage_config_changed': '📄 Конфиг страницы подписки изменён',
    # 2.8.0: новые события жизненного цикла API-токена панели (security-релевантно)
    'service.api_token_created': '🔑 Создан API-токен панели',
    'service.api_token_deleted': '🗝️ Удалён API-токен панели',
}

_ADMIN_CRM_EVENTS: dict[str, str] = {
    'crm.infra_billing_node_payment_in_7_days': '💳 Оплата ноды через 7 дней',
    'crm.infra_billing_node_payment_in_48hrs': '💳 Оплата ноды через 48 часов',
    'crm.infra_billing_node_payment_in_24hrs': '⚠️ Оплата ноды через 24 часа',
    'crm.infra_billing_node_payment_due_today': '🔴 Оплата ноды сегодня',
    'crm.infra_billing_node_payment_overdue_24hrs': '❗ Просрочка оплаты ноды: 24 часа',
    'crm.infra_billing_node_payment_overdue_48hrs': '❗ Просрочка оплаты ноды: 48 часов',
    'crm.infra_billing_node_payment_overdue_7_days': '🚨 Просрочка оплаты ноды: 7 дней',
}

_ADMIN_ERROR_EVENTS: dict[str, str] = {
    'errors.bandwidth_usage_threshold_reached_max_notifications': '⚠️ Достигнут лимит уведомлений о трафике',
}

_ADMIN_TORRENT_BLOCKER_EVENTS: dict[str, str] = {
    'torrent_blocker.report': '🚫 Торрент-блокировщик: обнаружен торрент',
}

_ADMIN_NODE_CONNECTION_EVENTS = frozenset({'node.connection_lost', 'node.connection_restored'})


class RemnaWaveWebhookService:
    """Processes incoming webhooks from RemnaWave backend."""

    # NOTE: In-memory guards. Only correct with a single-worker deployment.
    # For multi-worker setups, move to Redis or another shared store.
    _recent_recreations: dict[int, datetime] = {}
    _RECREATION_GUARD_SECONDS: int = 120  # 2-minute cooldown
    _intentional_panel_deletions_by_uuid: dict[str, datetime] = {}
    _intentional_panel_deletions_by_telegram_id: dict[int, datetime] = {}
    _INTENTIONAL_PANEL_DELETION_GUARD_SECONDS: int = 300
    _MAX_INTENTIONAL_ENTRIES: int = 10_000

    # Buffers bursts of node.connection_* webhooks (RemnaWave фаерит один
    # webhook на ноду при цикле websocket'а панели — раз в 3-4 часа это даёт
    # пик из 7-15 событий за пару секунд и трип flood control в Telegram).
    # Class-level defaults — fallback на случай если settings ещё не загружены;
    # __init__ перечитывает их из env REMNAWAVE_WEBHOOK_NODE_* и оверрайдит
    # на инстансе.
    _NODE_EVENT_COALESCE_WINDOW_SECONDS: float = 10.0
    # Жёсткий cap буфера на event_name. Защита от mem-DoS, если кто-то с
    # валидным REMNAWAVE_WEBHOOK_SECRET начнёт фаерить миллионы уникальных
    # событий за окно. Дедуп по (name, address) случается только при flush'е.
    # NB: cap PER event_name, не глобальный. С 2 event_name'ами (lost/restored)
    # worst-case в памяти ≈ 2× значение × ~3KB payload ≈ ~3MB суммарно.
    _NODE_EVENT_BUFFER_MAX: int = 500
    # Cap количества строк в сводном сообщении — у Telegram лимит 4096 символов
    # на сообщение, длинные списки нод выбивают TelegramBadRequest и теряются.
    _NODE_EVENT_SUMMARY_MAX_LINES: int = 40

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._admin_service = AdminNotificationService(bot)

        # Перечитываем coalescing-knob'ы из настроек (env-tunable, без
        # перезапуска кода). Class-level defaults остаются как safety net.
        self._NODE_EVENT_COALESCE_WINDOW_SECONDS = float(
            getattr(
                settings, 'REMNAWAVE_WEBHOOK_NODE_COALESCE_WINDOW_SECONDS', self._NODE_EVENT_COALESCE_WINDOW_SECONDS
            )
        )
        self._NODE_EVENT_BUFFER_MAX = int(
            getattr(settings, 'REMNAWAVE_WEBHOOK_NODE_BUFFER_MAX', self._NODE_EVENT_BUFFER_MAX)
        )

        # Per-instance coalescing state for node.connection_* events.
        # `_node_event_flush_task` — текущая «спящая» в окне coalescing задача.
        # `_node_event_pending_tasks` — strong-ref набор для GC-safety: задача
        # остаётся в нём до полного завершения коаллбэка через add_done_callback.
        # Asyncio docs предупреждают, что слабо-ссылочные задачи могут быть
        # выгружены GC до завершения; на CPython 3.13 риск практически нулевой,
        # но паттерн с set — канонический.
        self._node_event_buffer: dict[str, list[dict]] = {}
        self._node_event_overflow: dict[str, int] = {}
        self._node_event_flush_task: asyncio.Task[None] | None = None
        self._node_event_pending_tasks: set[asyncio.Task[None]] = set()
        self._node_event_lock = asyncio.Lock()
        # Set by stop(); blocks further enqueues so events arriving after the
        # graceful-shutdown drain don't create orphaned 10s-sleeping flush
        # tasks that the event loop will cancel mid-flight anyway.
        self._stopped: bool = False

        # User-scoped handlers: require user resolution
        self._user_handlers: dict[str, Any] = {
            'user.expired': self._handle_user_expired,
            'user.disabled': self._handle_user_disabled,
            'user.enabled': self._handle_user_enabled,
            'user.limited': self._handle_user_limited,
            'user.traffic_reset': self._handle_user_traffic_reset,
            'user.modified': self._handle_user_modified,
            'user.deleted': self._handle_user_deleted,
            'user.revoked': self._handle_user_revoked,
            'user.created': self._handle_user_created,
            # Старые (≤2.7.x) события об истечении — оставлены для обратной совместимости.
            'user.expires_in_72_hours': self._handle_expires_in_72h,
            'user.expires_in_48_hours': self._handle_expires_in_48h,
            'user.expires_in_24_hours': self._handle_expires_in_24h,
            'user.expired_24_hours_ago': self._handle_expired_24h_ago,
            # 2.8.0: единое событие, заменившее 4 выше (meta.expiration — знаковые часы).
            'user.expiration': self._handle_user_expiration,
            'user.first_connected': self._handle_first_connected,
            'user.bandwidth_usage_threshold_reached': self._handle_bandwidth_threshold,
            'user.not_connected': self._handle_user_not_connected,
            'user_hwid_devices.added': self._handle_device_added,
            'user_hwid_devices.deleted': self._handle_device_deleted,
            'torrent_blocker.report': self._handle_torrent_detected,
        }

        # Admin-scoped handlers: no user resolution, notify admin chat
        self._admin_handlers: dict[str, str] = {
            **_ADMIN_NODE_EVENTS,
            **_ADMIN_SERVICE_EVENTS,
            **_ADMIN_CRM_EVENTS,
            **_ADMIN_ERROR_EVENTS,
            **_ADMIN_TORRENT_BLOCKER_EVENTS,
        }

    def is_admin_event(self, event_name: str) -> bool:
        """Check if the event is admin-scoped (no DB session needed)."""
        return event_name in self._admin_handlers

    def needs_db_session(self, event_name: str) -> bool:
        """Check if the event requires a DB session (user handler or dual event)."""
        return event_name in self._user_handlers

    @classmethod
    def _prune_intentional_panel_deletions(cls) -> None:
        if not cls._intentional_panel_deletions_by_uuid and not cls._intentional_panel_deletions_by_telegram_id:
            return

        now = datetime.now(UTC)
        uuid_keys = [
            key
            for key, created_at in cls._intentional_panel_deletions_by_uuid.items()
            if (now - created_at).total_seconds() >= cls._INTENTIONAL_PANEL_DELETION_GUARD_SECONDS
        ]
        for key in uuid_keys:
            del cls._intentional_panel_deletions_by_uuid[key]

        telegram_keys = [
            key
            for key, created_at in cls._intentional_panel_deletions_by_telegram_id.items()
            if (now - created_at).total_seconds() >= cls._INTENTIONAL_PANEL_DELETION_GUARD_SECONDS
        ]
        for key in telegram_keys:
            del cls._intentional_panel_deletions_by_telegram_id[key]

    @classmethod
    def mark_intentional_panel_deletion(
        cls,
        *,
        panel_uuids: list[str] | None = None,
        telegram_id: int | None = None,
    ) -> None:
        cls._prune_intentional_panel_deletions()

        total = len(cls._intentional_panel_deletions_by_uuid) + len(cls._intentional_panel_deletions_by_telegram_id)
        if total >= cls._MAX_INTENTIONAL_ENTRIES:
            logger.warning('Intentional deletion guard at capacity, skipping', total=total)
            return

        now = datetime.now(UTC)

        for panel_uuid in panel_uuids or []:
            normalized = (panel_uuid or '').strip()
            if normalized:
                cls._intentional_panel_deletions_by_uuid[normalized] = now

        if telegram_id is not None:
            cls._intentional_panel_deletions_by_telegram_id[int(telegram_id)] = now

    @classmethod
    def _is_intentional_panel_deletion_event(cls, data: dict[str, Any]) -> bool:
        cls._prune_intentional_panel_deletions()

        candidate_uuids: list[str] = []
        candidate_telegram_ids: list[int] = []

        for value in (data.get('uuid'), data.get('userUuid')):
            if value:
                candidate_uuids.append(str(value).strip())

        telegram_id = data.get('telegramId')
        if telegram_id:
            try:
                candidate_telegram_ids.append(int(telegram_id))
            except (TypeError, ValueError):
                pass

        nested_user = data.get('user')
        if isinstance(nested_user, dict):
            nested_uuid = nested_user.get('uuid')
            if nested_uuid:
                candidate_uuids.append(str(nested_uuid).strip())

            nested_tid = nested_user.get('telegramId')
            if nested_tid:
                try:
                    candidate_telegram_ids.append(int(nested_tid))
                except (TypeError, ValueError):
                    pass

        return any(uid in cls._intentional_panel_deletions_by_uuid for uid in candidate_uuids) or any(
            tid in cls._intentional_panel_deletions_by_telegram_id for tid in candidate_telegram_ids
        )

    async def process_event(self, db: AsyncSession | None, event_name: str, data: dict) -> bool:
        """Route event to the appropriate handler.

        Returns True if the event was processed, False if skipped/unknown.
        db may be None for admin events that don't require database access.
        """
        # Check if event has both admin and user handlers (e.g. torrent_blocker.report)
        user_handler = self._user_handlers.get(event_name)
        if event_name in self._admin_handlers and user_handler:
            # Dual event: send admin notification AND process user handler
            await self._process_admin_event(event_name, data)
            if db is not None:
                await self._process_user_event(db, event_name, data, user_handler)
            return True

        # Check admin-scoped handlers (no DB needed)
        if event_name in self._admin_handlers:
            return await self._process_admin_event(event_name, data)

        # Check user-scoped handlers (require DB session)
        if user_handler:
            if db is None:
                logger.error('RemnaWave webhook: DB session required for user event', event_name=event_name)
                return False
            return await self._process_user_event(db, event_name, data, user_handler)

        logger.debug('Unhandled RemnaWave webhook event', event_name=event_name)
        return False

    async def _process_user_event(self, db: AsyncSession, event_name: str, data: dict, handler: Any) -> bool:
        """Resolve user and execute user-scoped handler."""
        user, subscription = await self._resolve_user_and_subscription(db, data)
        if not user:
            logger.warning(
                'RemnaWave webhook: user not found for event , data telegramId= uuid',
                event_name=event_name,
                data=data.get('telegramId'),
                data_2=data.get('uuid'),
            )
            return False

        user_id = user.id
        try:
            await handler(db, user, subscription, data)
            return True
        except (StaleDataError, PendingRollbackError):
            logger.warning(
                'RemnaWave webhook : entity already deleted for user (concurrent deletion)',
                event_name=event_name,
                user_id=user_id,
            )
            try:
                await db.rollback()
            except Exception:
                pass
            return True
        except Exception:
            logger.exception(
                'Error processing RemnaWave webhook event for user', event_name=event_name, user_id=user_id
            )
            try:
                await db.rollback()
            except Exception:
                logger.debug('Rollback after webhook handler error also failed')
            return False

    async def _process_admin_event(self, event_name: str, data: dict) -> bool:
        """Format and send admin notification for infrastructure events."""
        # Invalidate subscription page config cache on subpage config changes
        if event_name == 'service.subpage_config_changed':
            try:
                from app.handlers.subscription.common import invalidate_app_config_cache

                invalidate_app_config_cache()
                logger.info(
                    'Webhook: subpage config changed — app config cache invalidated',
                    action=data.get('subpageConfig', {}).get('action')
                    if isinstance(data.get('subpageConfig'), dict)
                    else None,
                )
            except Exception:
                logger.warning('Failed to invalidate app config cache on subpage_config_changed')

        if event_name in _ADMIN_NODE_CONNECTION_EVENTS and not settings.REMNAWAVE_WEBHOOK_NOTIFY_NODE_CONNECTION_STATUS:
            logger.debug('RemnaWave node connection notifications disabled, skipping event', event_name=event_name)
            return True

        if not self._admin_service.is_enabled:
            logger.debug('Admin notifications disabled, skipping event', event_name=event_name)
            return True

        # Coalesce bursts of node.connection_lost / node.connection_restored:
        # ставим в буфер, через окно вылетает одно сводное сообщение.
        if event_name in _ADMIN_NODE_CONNECTION_EVENTS:
            return await self._enqueue_node_event(event_name, data)

        title = self._admin_handlers.get(event_name, event_name)

        # Build message from event data (escape all untrusted values to prevent HTML injection)
        lines = [f'<b>{title}</b>']

        # Extract common fields. 2.8.0 service.api_token_* events nest the token
        # name under data.apiToken.name (see service.event.interface.ts).
        api_token = data.get('apiToken') if isinstance(data.get('apiToken'), dict) else {}
        name = html.escape(
            data.get('name') or data.get('nodeName') or data.get('username') or api_token.get('name') or ''
        )
        if name:
            lines.append(f'Имя: <code>{name}</code>')

        address = html.escape(data.get('address') or data.get('ip') or '')
        if address:
            lines.append(f'Адрес: <code>{address}</code>')

        port = data.get('port')
        if port:
            lines.append(f'Порт: <code>{html.escape(str(port))}</code>')

        version = html.escape(data.get('version') or data.get('panelVersion') or '')
        if version:
            lines.append(f'Версия: <code>{version}</code>')

        # CRM billing fields
        provider_name = html.escape(data.get('providerName') or '')
        if provider_name:
            lines.append(f'Провайдер: <code>{provider_name}</code>')

        amount = html.escape(str(data.get('amount') or data.get('price') or ''))
        if amount:
            lines.append(f'Сумма: <code>{amount}</code>')

        due_date = html.escape(data.get('dueDate') or data.get('paymentDate') or data.get('nextBillingAt') or '')
        if due_date:
            lines.append(f'Дата: <code>{due_date}</code>')

        login_url = data.get('loginUrl') or ''
        if login_url and self._is_valid_url(login_url):
            lines.append(f'Панель: {html.escape(login_url)}')

        # Login attempt fields
        login_data = data.get('loginAttempt')
        if isinstance(login_data, dict):
            login_user = html.escape(login_data.get('username') or '')
            if login_user:
                lines.append(f'Пользователь: <code>{login_user}</code>')
            login_ip = html.escape(login_data.get('ip') or '')
            if login_ip:
                lines.append(f'IP: <code>{login_ip}</code>')
            login_ua = html.escape(login_data.get('userAgent') or '')
            if login_ua:
                lines.append(f'User-Agent: <code>{login_ua[:100]}</code>')
            login_desc = html.escape(login_data.get('description') or '')
            if login_desc:
                lines.append(f'Описание: {login_desc}')
        else:
            ip_addr = html.escape(data.get('ipAddress') or data.get('ip') or '')
            if ip_addr and not address:
                lines.append(f'IP: <code>{ip_addr}</code>')

        message = html.escape(data.get('message') or data.get('description') or '')
        if message:
            lines.append(f'Сообщение: {message}')

        # Subpage config fields
        subpage = data.get('subpageConfig')
        if isinstance(subpage, dict):
            action = subpage.get('action', '')
            action_labels = {'CREATED': 'Создан', 'UPDATED': 'Обновлён', 'DELETED': 'Удалён'}
            lines.append(f'Действие: {action_labels.get(action, html.escape(str(action)))}')
            sub_uuid = subpage.get('uuid', '')
            if sub_uuid:
                lines.append(f'UUID: <code>{html.escape(str(sub_uuid))}</code>')

        # Torrent blocker fields
        if event_name == 'torrent_blocker.report':
            node_data = data.get('node')
            user_data = data.get('user')
            if isinstance(node_data, dict):
                node_name = html.escape(node_data.get('name') or '')
                if node_name:
                    lines.append(f'Нода: <code>{node_name}</code>')
                node_addr = html.escape(node_data.get('address') or '')
                if node_addr:
                    lines.append(f'Адрес: <code>{node_addr}</code>')
            if isinstance(user_data, dict):
                username = html.escape(user_data.get('username') or '')
                if username:
                    lines.append(f'Пользователь: <code>{username}</code>')
                user_status = html.escape(user_data.get('status') or '')
                if user_status:
                    lines.append(f'Статус: <code>{user_status}</code>')

        try:
            await self._admin_service.send_webhook_notification('\n'.join(lines))
            return True
        except Exception:
            logger.exception('Failed to send admin notification for event', event_name=event_name)
            return False

    # ------------------------------------------------------------------
    # Node connection event coalescing
    # ------------------------------------------------------------------

    async def _enqueue_node_event(self, event_name: str, data: dict) -> bool:
        """Buffer a node connection event for coalesced delivery."""
        async with self._node_event_lock:
            if self._stopped:
                # Бот завершает работу — drain уже прошёл, не плодим новые
                # фоновые таски, которые event loop всё равно отменит.
                logger.debug('Ignoring node event after stop()', event_name=event_name)
                return False
            bucket = self._node_event_buffer.setdefault(event_name, [])
            if len(bucket) >= self._NODE_EVENT_BUFFER_MAX:
                # Buffer overflow: считаем выкинутые события, попадёт в сводку.
                self._node_event_overflow[event_name] = self._node_event_overflow.get(event_name, 0) + 1
            else:
                bucket.append(data)
            if self._node_event_flush_task is None or self._node_event_flush_task.done():
                task = asyncio.create_task(self._flush_node_events_after_delay())
                self._node_event_flush_task = task
                # Set держит strong-ref всё время жизни таски; коллбэк удалит
                # её из набора после завершения. Защита от GC, плюс позволяет
                # aclose() корректно дождаться всех in-flight отправок.
                self._node_event_pending_tasks.add(task)
                task.add_done_callback(self._node_event_pending_tasks.discard)
        return True

    async def _flush_node_events_after_delay(self) -> None:
        """Sleep for the coalesce window, then flush every buffered event_name as one summary."""
        try:
            await asyncio.sleep(self._NODE_EVENT_COALESCE_WINDOW_SECONDS)
        except asyncio.CancelledError:
            return

        async with self._node_event_lock:
            buffer = self._node_event_buffer
            overflow = self._node_event_overflow
            self._node_event_buffer = {}
            self._node_event_overflow = {}
            self._node_event_flush_task = None

        for event_name, payloads in buffer.items():
            try:
                await self._send_coalesced_node_notification(
                    event_name, payloads, overflow_count=overflow.get(event_name, 0)
                )
            except Exception:
                logger.exception(
                    'Failed to send coalesced node notification',
                    event_name=event_name,
                    count=len(payloads),
                )

    async def _send_coalesced_node_notification(
        self,
        event_name: str,
        payloads: list[dict],
        *,
        overflow_count: int = 0,
    ) -> None:
        """Build a single summary message for N node events of the same type."""
        title = self._admin_handlers.get(event_name, event_name)
        max_lines = self._NODE_EVENT_SUMMARY_MAX_LINES

        # Dedupe by (name, address) — RemnaWave иногда ретраит один и тот же
        # webhook, плюс это даёт компактный список даже если ноды действительно
        # упали все разом.
        seen: set[tuple[str, str]] = set()
        node_lines: list[str] = []
        for data in payloads:
            name = (data.get('name') or data.get('nodeName') or '').strip()
            address = (data.get('address') or data.get('ip') or '').strip()
            key = (name, address)
            if key in seen:
                continue
            seen.add(key)

            if len(node_lines) >= max_lines:
                # Считаем уникальных дальше, но в текст не добавляем —
                # лимит 4096 символов у Telegram, длинные списки уйдут в /dev/null.
                continue

            descr = f'<code>{html.escape(name)}</code>' if name else '<i>без имени</i>'
            if address:
                descr += f' (<code>{html.escape(address)}</code>)'
            node_lines.append(f'• {descr}')

        if not node_lines and not overflow_count:
            return

        unique_count = len(seen)
        truncated = unique_count - len(node_lines)
        if truncated > 0:
            node_lines.append(f'• <i>… ещё {truncated} нод(ы) (truncated)</i>')
        if overflow_count > 0:
            node_lines.append(f'• <i>… ещё {overflow_count} событий отброшено (buffer overflow)</i>')

        total = unique_count + overflow_count
        header = f'<b>{title}</b>' if total <= 1 else f'<b>{title} × {total}</b>'
        text = header + '\n' + '\n'.join(node_lines)

        # send_webhook_notification возвращает bool и сама ловит исключения —
        # отдельный try/except здесь только дублирует логи; внешний
        # _flush_node_events_after_delay уже ловит любые сюрпризы.
        await self._admin_service.send_webhook_notification(text)

    # Жёсткий cap на общую длительность stop() — выше container'овского
    # terminationGracePeriodSeconds выходить нельзя, иначе SIGKILL.
    _STOP_DRAIN_TIMEOUT_SECONDS: float = 15.0
    _STOP_CANCEL_TIMEOUT_SECONDS: float = 2.0

    async def stop(self) -> None:
        """Drain buffered node events before shutdown.

        Cancels the in-flight «sleep + flush» task (если она ещё в окне
        coalescing'а), потом вручную свопает буфер и шлёт по одному сводному
        сообщению на event_name. Без этого SIGTERM/lifespan shutdown терял
        бы до 10 секунд накопленных webhook'ов в памяти.

        Trade-off: если флаш-таска была уже за пределами sleep'а и в момент
        отмены крутилась внутри `bot.send_message`, её локальный `buffer`
        теряется вместе с CancelledError, и часть событий из ТОЙ партии
        может не дойти до Telegram. События, накопившиеся после её свопа,
        дренируются здесь явно.

        Дрейн ограничен таймаутом, чтобы медленный Telegram не выбил процесс
        за terminationGracePeriodSeconds.

        После выхода из stop() новые enqueue'ы дропаются (см. `_stopped`),
        чтобы не плодить orphaned flush-таски, которые event loop отменит.
        """
        # Помечаем сервис остановленным под локом, чтобы любой in-flight
        # enqueue либо успел до нас (и попадёт в drain), либо увидит флаг.
        async with self._node_event_lock:
            self._stopped = True

        task = self._node_event_flush_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=self._STOP_CANCEL_TIMEOUT_SECONDS)
            except (TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                # Реальная ошибка в задаче — логируем как warning, чтобы
                # IGNORED_LOGGER_PREFIXES (см. logging_handler.py) не свалил
                # это обратно в админ-чат через TelegramNotifierProcessor.
                logger.warning(
                    'Flush task raised during stop()',
                    exc_info=True,
                )

        async with self._node_event_lock:
            buffer = self._node_event_buffer
            overflow = self._node_event_overflow
            self._node_event_buffer = {}
            self._node_event_overflow = {}
            self._node_event_flush_task = None

        if not buffer:
            return

        try:
            await asyncio.wait_for(
                self._drain_buffered_events(buffer, overflow),
                timeout=self._STOP_DRAIN_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                'Node-event drain timed out on stop()',
                event_types=list(buffer.keys()),
                total_events=sum(len(payloads) for payloads in buffer.values()),
            )

    async def _drain_buffered_events(
        self,
        buffer: dict[str, list[dict]],
        overflow: dict[str, int],
    ) -> None:
        """Best-effort send of all buffered events; called by stop() under a timeout."""
        for event_name, payloads in buffer.items():
            try:
                await self._send_coalesced_node_notification(
                    event_name, payloads, overflow_count=overflow.get(event_name, 0)
                )
            except Exception:
                logger.exception(
                    'Failed to flush node events during shutdown',
                    event_name=event_name,
                    count=len(payloads),
                )

    # ------------------------------------------------------------------
    # User resolution
    # ------------------------------------------------------------------

    async def _resolve_user_and_subscription(
        self, db: AsyncSession, data: dict
    ) -> tuple[User | None, Subscription | None]:
        """Find bot user by telegramId or uuid from webhook payload.

        Handles both user-scope events (top-level telegramId/uuid) and
        device-scope events (userUuid, or nested user.telegramId/user.uuid).

        In multi-tariff mode, resolves subscription by remnawave_uuid from payload
        (each subscription has its own Remnawave user).
        """
        user: User | None = None
        remnawave_uuid: str | None = None

        # Extract Remnawave UUID from payload (used for subscription lookup in multi-tariff)
        remnawave_uuid = data.get('uuid') or data.get('userUuid')
        if not remnawave_uuid:
            nested_user = data.get('user')
            if isinstance(nested_user, dict):
                remnawave_uuid = nested_user.get('uuid')

        # Try top-level telegramId first
        telegram_id = data.get('telegramId')
        if telegram_id:
            try:
                user = await get_user_by_telegram_id(db, int(telegram_id))
            except (ValueError, TypeError):
                pass

        # Try top-level uuid
        if not user and remnawave_uuid:
            user = await get_user_by_remnawave_uuid(db, remnawave_uuid)

        # Try nested user object (e.g. user_hwid_devices events)
        if not user:
            nested_user = data.get('user')
            if isinstance(nested_user, dict):
                nested_tid = nested_user.get('telegramId')
                if nested_tid:
                    try:
                        user = await get_user_by_telegram_id(db, int(nested_tid))
                    except (ValueError, TypeError):
                        pass
                if not user:
                    nested_uuid = nested_user.get('uuid')
                    if nested_uuid:
                        user = await get_user_by_remnawave_uuid(db, nested_uuid)

        # Multi-tariff: try finding user through subscription's remnawave_uuid
        if not user and remnawave_uuid and settings.is_multi_tariff_enabled():
            from sqlalchemy import select as sa_select
            from sqlalchemy.orm import selectinload as sa_selectinload

            sub_result = await db.execute(
                sa_select(Subscription)
                .options(
                    sa_selectinload(Subscription.user)
                    .selectinload(User.subscriptions)
                    .selectinload(Subscription.tariff),
                    sa_selectinload(Subscription.tariff),
                )
                .where(Subscription.remnawave_uuid == remnawave_uuid)
                .limit(1)
            )
            found_sub = sub_result.scalar_one_or_none()
            if found_sub and found_sub.user:
                return found_sub.user, found_sub

        if not user:
            return None, None

        # In multi-tariff mode, find subscription by remnawave_uuid (per-subscription)
        if settings.is_multi_tariff_enabled() and remnawave_uuid:
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload

            result = await db.execute(
                select(Subscription)
                .options(selectinload(Subscription.tariff))
                .where(
                    Subscription.remnawave_uuid == remnawave_uuid,
                    Subscription.user_id == user.id,
                )
            )
            subscription = result.scalar_one_or_none()
            if subscription:
                return user, subscription

            # Fallback 1: search ALL user's subscriptions by remnawave_uuid
            # (covers recently merged accounts where user_id might differ)
            logger.warning(
                'Webhook: подписка не найдена по remnawave_uuid + user_id, '
                'fallback на поиск по remnawave_uuid среди всех подписок пользователя',
                remnawave_uuid=remnawave_uuid,
                user_id=user.id,
            )
            fallback1_result = await db.execute(
                select(Subscription)
                .options(selectinload(Subscription.tariff))
                .where(Subscription.remnawave_uuid == remnawave_uuid)
                .limit(1)
            )
            fallback1_sub = fallback1_result.scalar_one_or_none()
            if fallback1_sub:
                if fallback1_sub.user_id == user.id:
                    return user, fallback1_sub
                # Subscription belongs to a different user (transferred or merged)
                logger.warning(
                    'Webhook: подписка найдена по remnawave_uuid, '
                    'но принадлежит другому пользователю — игнорируем (IDOR prevention)',
                    remnawave_uuid=remnawave_uuid,
                    webhook_user_id=user.id,
                    subscription_user_id=fallback1_sub.user_id,
                    subscription_id=fallback1_sub.id,
                )
                # Do NOT return cross-user subscription — would mutate another user's data
                return user, None

            # Fallback 2: all lookups exhausted
            logger.warning(
                'Webhook: подписка не найдена ни по одному методу поиска, возвращаем (user, None)',
                remnawave_uuid=remnawave_uuid,
                user_id=user.id,
            )

        if settings.is_multi_tariff_enabled():
            # In multi-tariff mode, don't fall back to arbitrary subscription
            return user, None
        subscription = await get_subscription_by_user_id(db, user.id)
        return user, subscription

    # ------------------------------------------------------------------
    # Notification helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_valid_url(value: str) -> bool:
        """Basic URL validation to prevent stored XSS via crafted URLs."""
        if not value or len(value) > 2048:
            return False
        return bool(re.match(r'^https?://', value))

    @staticmethod
    def _is_valid_link(value: str) -> bool:
        """Validate URL or deep link (happ://, vless://, ss://, etc.)."""
        if not value or len(value) > 4096:
            return False
        return bool(re.match(r'^[a-zA-Z][a-zA-Z0-9+\-.]*://', value))

    def _get_renew_keyboard(self, user: User, subscription_id: int | None = None) -> InlineKeyboardMarkup:
        texts = get_texts(user.language)
        button_text = texts.get('WEBHOOK_RENEW_BUTTON', 'Renew subscription')
        extend_callback = (
            f'se:{subscription_id}' if settings.is_multi_tariff_enabled() and subscription_id else 'subscription_extend'
        )
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [build_miniapp_or_callback_button(text=button_text, callback_data=extend_callback)],
            ]
        )

    def _get_subscription_keyboard(self, user: User) -> InlineKeyboardMarkup:
        texts = get_texts(user.language)
        button_text = texts.get('MY_SUBSCRIPTION_BUTTON', 'My subscription')
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [build_miniapp_or_callback_button(text=button_text, callback_data='menu_subscription')],
            ]
        )

    def _get_connect_keyboard(self, user: User) -> InlineKeyboardMarkup:
        texts = get_texts(user.language)
        button_text = texts.get('CONNECT_BUTTON', 'Connect')
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [build_miniapp_or_callback_button(text=button_text, callback_data='subscription_connect')],
            ]
        )

    def _get_traffic_keyboard(self, user: User) -> InlineKeyboardMarkup:
        texts = get_texts(user.language)
        buy_text = texts.get('BUY_TRAFFIC_BUTTON', 'Buy traffic')
        sub_text = texts.get('MY_SUBSCRIPTION_BUTTON', 'My subscription')
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [build_miniapp_or_callback_button(text=buy_text, callback_data='buy_traffic')],
                [build_miniapp_or_callback_button(text=sub_text, callback_data='menu_subscription')],
            ]
        )

    async def _notify_user(
        self,
        user: User,
        text_key: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        format_kwargs: dict[str, Any] | None = None,
        subscription: Subscription | None = None,
    ) -> None:
        """Send a notification to user via appropriate channel.

        Telegram users receive a bot message; email-only users receive
        an email and/or WebSocket notification through the unified
        notification delivery service.

        Respects WEBHOOK_NOTIFY_USER_ENABLED master toggle and
        per-event toggles from Settings.
        """
        if not settings.WEBHOOK_NOTIFY_USER_ENABLED:
            logger.debug('Webhook user notifications disabled globally, skipping', text_key=text_key)
            return

        setting_key = _TEXT_KEY_TO_SETTING.get(text_key)
        if setting_key and not getattr(settings, setting_key, True):
            logger.debug('Webhook notification disabled via', text_key=text_key, setting_key=setting_key)
            return

        texts = get_texts(user.language)
        message = texts.get(text_key)
        if not message:
            logger.warning('Missing locale key for language', text_key=text_key, language=user.language)
            return

        # Inject tariff_label for multi-tariff subscription identification
        if format_kwargs is None:
            format_kwargs = {}
        if 'tariff_label' not in format_kwargs:
            tariff_label = ''
            if settings.is_multi_tariff_enabled() and subscription:
                # Access tariff only if already eagerly loaded to avoid
                # MissingGreenlet from lazy loading in async context
                loaded_tariff = sa_inspect(subscription).dict.get('tariff')
                if loaded_tariff is not None:
                    tariff_label = f' «{loaded_tariff.name}»'
            format_kwargs['tariff_label'] = tariff_label

        if format_kwargs:
            try:
                message = message.format(**format_kwargs)
            except (KeyError, IndexError):
                logger.warning('Failed to format message with kwargs', text_key=text_key, format_kwargs=format_kwargs)
                return

        # Append "Close" button to every webhook notification keyboard
        close_text = texts.get('WEBHOOK_CLOSE_BUTTON', '✖️ Закрыть')
        close_row = [InlineKeyboardButton(text=close_text, callback_data='webhook:close')]
        if reply_markup:
            reply_markup = InlineKeyboardMarkup(
                inline_keyboard=[*reply_markup.inline_keyboard, close_row],
            )
        else:
            reply_markup = InlineKeyboardMarkup(inline_keyboard=[close_row])

        notification_type = _TEXT_KEY_TO_NOTIFICATION_TYPE.get(text_key)
        if not notification_type:
            logger.warning('No NotificationType mapping for text_key', text_key=text_key)
            return

        context = {'text_key': text_key, **(format_kwargs or {})}

        try:
            await notification_delivery_service.send_notification(
                user=user,
                notification_type=notification_type,
                context=context,
                bot=self.bot,
                telegram_message=message,
                telegram_markup=reply_markup,
            )
        except Exception:
            logger.exception('Notification delivery failed for user , text_key', user_id=user.id, text_key=text_key)

    # ------------------------------------------------------------------
    # Webhook timestamp helper
    # ------------------------------------------------------------------

    @staticmethod
    def _stamp_webhook_update(subscription: Subscription) -> None:
        """Mark subscription as recently updated by webhook to prevent sync overwrite."""
        subscription.last_webhook_update_at = datetime.now(UTC)

    # ------------------------------------------------------------------
    # User event handlers
    # ------------------------------------------------------------------

    async def _handle_user_expired(
        self, db: AsyncSession, user: User, subscription: Subscription | None, data: dict
    ) -> None:
        if not subscription:
            # Подписка уже удалена из БД — фантомный хук от панели, игнорируем
            logger.info('Webhook user.expired: подписка не найдена в БД (уже удалена), пропуск', user_id=user.id)
            return

        # Суточные подписки управляются DailySubscriptionService.
        # Remnawave может прислать user.expired если sync не дошёл (старый end_date),
        # но локально подписка ещё жива — не экспайрим её.
        tariff = sa_inspect(subscription).dict.get('tariff')
        is_active_daily = (
            tariff is not None
            and getattr(tariff, 'is_daily', False)
            and not getattr(subscription, 'is_daily_paused', False)
        )
        if is_active_daily:
            logger.info(
                'Webhook: пропуск expire для суточной подписки (управляет DailySubscriptionService)',
                subscription_id=subscription.id,
                user_id=user.id,
            )
            self._stamp_webhook_update(subscription)
            await db.commit()
            return

        self._stamp_webhook_update(subscription)
        if subscription.status != SubscriptionStatus.EXPIRED.value:
            await expire_subscription(db, subscription)
            logger.info('Webhook: subscription expired for user', subscription_id=subscription.id, user_id=user.id)
        else:
            await db.commit()

        await self._notify_user(
            user,
            'WEBHOOK_SUB_EXPIRED',
            reply_markup=self._get_renew_keyboard(user, subscription.id),
            subscription=subscription,
        )

    async def _handle_user_disabled(
        self, db: AsyncSession, user: User, subscription: Subscription | None, data: dict
    ) -> None:
        if not subscription:
            # Подписка уже удалена из БД — фантомный хук от панели, игнорируем
            logger.info('Webhook user.disabled: подписка не найдена в БД (уже удалена), пропуск', user_id=user.id)
            return

        # Суточные подписки управляются DailySubscriptionService — не деактивируем
        tariff = sa_inspect(subscription).dict.get('tariff')
        is_active_daily = (
            tariff is not None
            and getattr(tariff, 'is_daily', False)
            and not getattr(subscription, 'is_daily_paused', False)
        )
        if is_active_daily:
            logger.info(
                'Webhook: пропуск disabled для суточной подписки',
                subscription_id=subscription.id,
                user_id=user.id,
            )
            self._stamp_webhook_update(subscription)
            await db.commit()
            return

        # Защита от echo-webhook: если подписка была недавно реактивирована
        # (канал-реподписка ставит last_webhook_update_at), пропускаем
        if subscription.status == SubscriptionStatus.ACTIVE.value and is_recently_updated_by_webhook(subscription):
            logger.info(
                'Webhook user.disabled: подписка недавно реактивирована, пропуск echo-webhook',
                subscription_id=subscription.id,
                user_id=user.id,
            )
            self._stamp_webhook_update(subscription)
            await db.commit()
            return

        self._stamp_webhook_update(subscription)
        if subscription.status != SubscriptionStatus.DISABLED.value:
            await deactivate_subscription(db, subscription)
            logger.info('Webhook: subscription disabled for user', subscription_id=subscription.id, user_id=user.id)
        else:
            await db.commit()

        await self._notify_user(
            user, 'WEBHOOK_SUB_DISABLED', reply_markup=self._get_subscription_keyboard(user), subscription=subscription
        )

    async def _handle_user_enabled(
        self, db: AsyncSession, user: User, subscription: Subscription | None, data: dict
    ) -> None:
        if not subscription:
            logger.info('Webhook user.enabled: подписка не найдена в БД (уже удалена), пропуск', user_id=user.id)
            return

        self._stamp_webhook_update(subscription)
        if subscription.status in (SubscriptionStatus.DISABLED.value, SubscriptionStatus.LIMITED.value):
            await reactivate_subscription(db, subscription)
            logger.info('Webhook: subscription re-enabled for user', subscription_id=subscription.id, user_id=user.id)
        else:
            await db.commit()

        await self._notify_user(
            user, 'WEBHOOK_SUB_ENABLED', reply_markup=self._get_connect_keyboard(user), subscription=subscription
        )

    async def _handle_user_limited(
        self, db: AsyncSession, user: User, subscription: Subscription | None, data: dict
    ) -> None:
        if not subscription:
            logger.info('Webhook user.limited: подписка не найдена в БД (уже удалена), пропуск', user_id=user.id)
            return

        self._stamp_webhook_update(subscription)
        if subscription.status in (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value):
            subscription.status = SubscriptionStatus.LIMITED.value
            subscription.updated_at = datetime.now(UTC)
            await db.commit()
            await db.refresh(subscription)
            logger.info(
                'Webhook: subscription limited (traffic) for user', subscription_id=subscription.id, user_id=user.id
            )
        else:
            await db.commit()

        await self._notify_user(
            user, 'WEBHOOK_SUB_LIMITED', reply_markup=self._get_traffic_keyboard(user), subscription=subscription
        )

    async def _handle_user_traffic_reset(
        self, db: AsyncSession, user: User, subscription: Subscription | None, data: dict
    ) -> None:
        if not subscription:
            logger.info('Webhook user.traffic_reset: подписка не найдена в БД (уже удалена), пропуск', user_id=user.id)
            return

        self._stamp_webhook_update(subscription)
        await update_subscription_usage(db, subscription, 0.0)
        # Re-enable if was disabled/limited due to traffic limit
        if subscription.status in (SubscriptionStatus.DISABLED.value, SubscriptionStatus.LIMITED.value):
            await reactivate_subscription(db, subscription)
        logger.info('Webhook: traffic reset for subscription , user', subscription_id=subscription.id, user_id=user.id)

        await self._notify_user(
            user,
            'WEBHOOK_SUB_TRAFFIC_RESET',
            reply_markup=self._get_subscription_keyboard(user),
            subscription=subscription,
        )

    async def _handle_user_modified(
        self, db: AsyncSession, user: User, subscription: Subscription | None, data: dict
    ) -> None:
        """Sync subscription fields from webhook payload without notifying user."""
        if not subscription:
            return

        changed = False

        # Sync traffic limit
        traffic_limit_bytes = data.get('trafficLimitBytes')
        if traffic_limit_bytes is not None:
            try:
                new_limit_gb = int(traffic_limit_bytes) // (1024**3)
                if subscription.traffic_limit_gb != new_limit_gb:
                    subscription.traffic_limit_gb = new_limit_gb
                    changed = True
            except (ValueError, TypeError):
                pass

        # Sync used traffic. usedTrafficBytes живёт в nested userTraffic
        # (ExtendedUsersSchema.userTraffic; базовый UsersSchema плоского поля не
        # содержит) — читаем nested-first, как _get_user_traffic_bytes в sync-сервисе,
        # с fallback на плоский ключ для старых панелей. Без этого used-traffic не
        # синхронизировался из user.modified-вебхуков (поле всегда было None).
        user_traffic = data.get('userTraffic')
        used_traffic_bytes = (
            user_traffic.get('usedTrafficBytes')
            if isinstance(user_traffic, dict) and user_traffic.get('usedTrafficBytes') is not None
            else data.get('usedTrafficBytes')
        )
        if used_traffic_bytes is not None:
            try:
                new_used_gb = round(int(used_traffic_bytes) / (1024**3), 2)
                subscription.traffic_used_gb = new_used_gb
                changed = True
            except (ValueError, TypeError):
                pass

        # Sync expire date — panel is the source of truth for user.modified events.
        # НО: если подписка намеренно ОТКЛЮЧЕНА в боте (обнуление/деактивация админом),
        # не воскрешаем её срок из устаревшего panel expireAt — иначе наспамленные дни
        # могли бы «вернуться» после обнуления (см. crud.reset_subscription). Статус
        # отдельно синхронизируется ниже: при panel ACTIVE + future end_date подписка
        # всё равно может корректно реактивироваться через обычное продление/активацию.
        expire_at = data.get('expireAt')
        if expire_at and subscription.status != SubscriptionStatus.DISABLED.value:
            try:
                parsed_dt = datetime.fromisoformat(expire_at.replace('Z', '+00:00'))
                new_end_date = parsed_dt.astimezone(UTC)
                if subscription.end_date != new_end_date:
                    old_end_date = subscription.end_date
                    subscription.end_date = new_end_date
                    changed = True
                    if old_end_date and new_end_date < old_end_date:
                        logger.info(
                            'Webhook: end_date обновлена назад (панель авторитетна)',
                            subscription_id=subscription.id,
                            old_end_date=old_end_date,
                            new_end_date=new_end_date,
                        )
            except (ValueError, TypeError):
                pass

        # Sync status from panel
        panel_status = data.get('status')
        if panel_status:
            now = datetime.now(UTC)
            end_date = subscription.end_date
            if panel_status == 'ACTIVE' and end_date and end_date > now:
                if subscription.status != SubscriptionStatus.ACTIVE.value:
                    subscription.status = SubscriptionStatus.ACTIVE.value
                    changed = True
                    logger.info(
                        'Webhook: subscription reactivated (→ active) for user',
                        subscription_id=subscription.id,
                        subscription_status=subscription.status,
                        user_id=user.id,
                    )
            elif panel_status == 'DISABLED':
                if subscription.status != SubscriptionStatus.DISABLED.value:
                    subscription.status = SubscriptionStatus.DISABLED.value
                    changed = True

        # Sync subscription URL (validate to prevent stored XSS)
        subscription_url = data.get('subscriptionUrl')
        if (
            subscription_url
            and self._is_valid_url(subscription_url)
            and subscription.subscription_url != subscription_url
        ):
            subscription.subscription_url = subscription_url
            changed = True

        # Sync subscription crypto link (for HAPP_CRYPT4_LINK)
        subscription_crypto_link = data.get('subscriptionCryptoLink') or (data.get('happ') or {}).get('cryptoLink', '')
        if subscription_crypto_link and self._is_valid_link(subscription_crypto_link):
            if subscription.subscription_crypto_link != subscription_crypto_link:
                subscription.subscription_crypto_link = subscription_crypto_link
                changed = True
        # NOTE: панель не включает cryptoLink в каждый webhook user.modified
        # Отсутствие поля не означает что его нужно сбрасывать

        # Always stamp to protect from sync overwrite, even if no fields changed
        self._stamp_webhook_update(subscription)
        if changed:
            subscription.updated_at = datetime.now(UTC)
            logger.info(
                'Webhook: subscription modified (synced from panel) for user',
                subscription_id=subscription.id,
                user_id=user.id,
            )
        await db.commit()

    async def _handle_user_deleted(
        self, db: AsyncSession, user: User, subscription: Subscription | None, data: dict
    ) -> None:
        # Suppress webhook if this deletion was initiated by delete_user_account —
        # prevents deadlock between the ongoing deletion transaction and this handler
        if self._is_intentional_panel_deletion_event(data):
            logger.info(
                'Webhook user.deleted suppressed — intentional panel deletion in progress',
                user_id=user.id,
                uuid=data.get('uuid'),
            )
            return

        user_id = user.id
        sub_id = subscription.id if subscription else None

        # Evict stale entries from the recreation loop guard to prevent unbounded growth
        if self._recent_recreations:
            now = datetime.now(UTC)
            expired_keys = [
                k
                for k, v in self._recent_recreations.items()
                if (now - v).total_seconds() >= self._RECREATION_GUARD_SECONDS
            ]
            for k in expired_keys:
                del self._recent_recreations[k]

        # Guard against webhook loop: if we recently attempted panel recreation for this
        # subscription (from a previous user.deleted), skip to prevent unbounded
        # recreate→delete→recreate cycles. Uses an in-memory guard (not the generic
        # last_webhook_update_at stamp which fires on ANY webhook event).
        if sub_id and sub_id in self._recent_recreations:
            elapsed = (datetime.now(UTC) - self._recent_recreations[sub_id]).total_seconds()
            if elapsed < self._RECREATION_GUARD_SECONDS:
                logger.warning(
                    'Webhook user.deleted: skipping — panel recreation was attempted recently (recreation loop guard)',
                    sub_id=sub_id,
                    user_id=user_id,
                    elapsed=round(elapsed, 1),
                )
                return

        # Stamp immediately (before any await) so concurrent coroutines see the guard
        if sub_id:
            self._recent_recreations[sub_id] = datetime.now(UTC)

        if subscription:
            self._stamp_webhook_update(subscription)

            # Decrement server counters BEFORE clearing connected_squads
            await decrement_subscription_server_counts(db, subscription)

            # Re-fetch after potential rollback inside decrement_subscription_server_counts
            try:
                await db.refresh(subscription)
            except Exception:
                # Subscription was cascade-deleted, re-fetch user and skip subscription updates
                logger.warning(
                    'Webhook: subscription already deleted for user , skipping subscription cleanup',
                    sub_id=sub_id,
                    user_id=user_id,
                )
                subscription = None
                try:
                    await db.rollback()
                except Exception:
                    pass

                try:
                    user = await get_user_by_id(db, user_id)
                except Exception:
                    logger.error('Webhook: user not found after rollback', user_id=user_id)
                    return
                if not user:
                    logger.error('Webhook: user not found after rollback', user_id=user_id)
                    return

        # user.deleted = user removed from panel. Deactivate everything.
        # No recreation attempts — if it was a mistake, admin can re-sync.

        if subscription:
            if subscription.status != SubscriptionStatus.EXPIRED.value:
                subscription.status = SubscriptionStatus.EXPIRED.value
                logger.info(
                    'Webhook user.deleted: subscription expired',
                    sub_id=sub_id,
                    user_id=user_id,
                )
            subscription.subscription_url = None
            subscription.subscription_crypto_link = None
            subscription.remnawave_short_uuid = None
            subscription.connected_squads = []
            subscription.updated_at = datetime.now(UTC)

            # Always clear stale UUID — panel user was deleted
            subscription.remnawave_uuid = None

            await db.execute(delete(SubscriptionServer).where(SubscriptionServer.subscription_id == sub_id))

        # Clear remnawave linkage
        if not settings.is_multi_tariff_enabled():
            if user.remnawave_uuid:
                user.remnawave_uuid = None
        elif subscription is None:
            panel_uuid = data.get('uuid') or data.get('userUuid')
            if panel_uuid:
                await db.refresh(user, ['subscriptions'])
                for sub in getattr(user, 'subscriptions', None) or []:
                    if getattr(sub, 'remnawave_uuid', None) == panel_uuid:
                        sub.remnawave_uuid = None
                        sub.remnawave_short_uuid = None
                        break

        # Deactivate sibling subscriptions whose panel user also no longer exists.
        # In multi-tariff each subscription has its own panel user — only expire those
        # that are actually gone (verified via API), leave alive ones untouched.
        await db.refresh(user, ['subscriptions'])
        now = datetime.now(UTC)
        from app.database.models import _aware
        from app.services.subscription_service import SubscriptionService

        subscription_service = SubscriptionService()
        for other_sub in getattr(user, 'subscriptions', None) or []:
            if other_sub.id == sub_id:
                continue
            if other_sub.status in (SubscriptionStatus.EXPIRED.value, SubscriptionStatus.DISABLED.value):
                continue

            # Never expire a sibling that is still valid by its own end_date. Another
            # subscription's panel user being deleted must not retroactively kill a
            # paid, not-yet-expired sub — e.g. a pre-multi-tariff sub whose panel UUID
            # lives on user.remnawave_uuid. (Bug: deleting a 2nd, expired sub expired
            # the original active one and wiped its squads.)
            other_end = _aware(other_sub.end_date)
            if other_end is not None and other_end > now:
                continue

            # Resolve the sibling's panel UUID — fall back to user.remnawave_uuid in
            # BOTH modes, since pre-multi-tariff subs store the panel UUID there.
            sibling_uuid = getattr(other_sub, 'remnawave_uuid', None) or getattr(user, 'remnawave_uuid', None)

            # Only expire when the panel POSITIVELY reports the user is gone. If we
            # cannot verify (no uuid, API not configured, or a transient error), leave
            # the sub untouched — an unverifiable check must never expire a live sub.
            if not sibling_uuid or not subscription_service.is_configured:
                continue
            try:
                async with subscription_service.get_api_client() as api:
                    panel_user = await api.get_user_by_uuid(sibling_uuid)
            except Exception as exc:
                logger.warning(
                    'Webhook user.deleted: sibling liveness check failed, leaving subscription untouched',
                    other_sub_id=other_sub.id,
                    error=str(exc),
                )
                continue
            if panel_user is not None:
                continue  # still alive in panel, don't touch

            other_sub.status = SubscriptionStatus.EXPIRED.value
            other_sub.subscription_url = None
            other_sub.subscription_crypto_link = None
            other_sub.remnawave_short_uuid = None
            other_sub.connected_squads = []
            other_sub.updated_at = now
            if settings.is_multi_tariff_enabled():
                other_sub.remnawave_uuid = None
            await db.execute(delete(SubscriptionServer).where(SubscriptionServer.subscription_id == other_sub.id))
            logger.info(
                'Webhook user.deleted: deactivated sibling subscription (panel user gone)',
                other_sub_id=other_sub.id,
                user_id=user_id,
            )

        await db.commit()

        await self._notify_user(
            user,
            'WEBHOOK_SUB_DELETED',
            reply_markup=self._get_renew_keyboard(user, getattr(subscription, 'id', None) if subscription else None),
            subscription=subscription,
        )

    async def _attempt_panel_recreation(self, db: AsyncSession, user: User, subscription: Subscription) -> bool:
        """Re-create user in RemnaWave panel after spurious user.deleted webhook.

        Called when a user.deleted webhook arrives but the subscription still has a
        future end_date, indicating the deletion was likely spurious (e.g., RemnaWave
        resync when modifying another user). Attempts to restore VPN access by
        creating/updating the user in the panel.

        Returns True if recreation succeeded, False otherwise.
        """
        # Update the recreation guard timestamp to the actual recreation start time
        if subscription.id is not None:
            self._recent_recreations[subscription.id] = datetime.now(UTC)

        try:
            from app.services.subscription_service import SubscriptionService

            service = SubscriptionService()
            if not service.is_configured:
                logger.warning(
                    'RemnaWave not configured, cannot re-create panel user after user.deleted',
                    user_id=user.id,
                )
                return False

            remnawave_user = await service.create_remnawave_user(db, subscription)
            if remnawave_user:
                logger.info(
                    'Webhook user.deleted: successfully re-created user in panel',
                    user_id=user.id,
                    subscription_id=subscription.id,
                    new_uuid=remnawave_user.uuid,
                )
                return True

            logger.error(
                'Webhook user.deleted: failed to re-create user in panel',
                user_id=user.id,
                subscription_id=subscription.id,
            )
            return False
        except Exception as e:
            logger.error(
                'Webhook user.deleted: error re-creating user in panel',
                user_id=user.id,
                subscription_id=subscription.id,
                error=e,
            )
            return False

    async def _handle_user_revoked(
        self, db: AsyncSession, user: User, subscription: Subscription | None, data: dict
    ) -> None:
        if not subscription:
            logger.info('Webhook user.revoked: подписка не найдена в БД, пропуск', user_id=user.id)
            return

        new_url = data.get('subscriptionUrl')
        new_crypto_link = data.get('subscriptionCryptoLink') or (data.get('happ') or {}).get('cryptoLink', '')
        changed = False

        if new_url and self._is_valid_url(new_url) and subscription.subscription_url != new_url:
            subscription.subscription_url = new_url
            changed = True
        if new_crypto_link and self._is_valid_link(new_crypto_link):
            if subscription.subscription_crypto_link != new_crypto_link:
                subscription.subscription_crypto_link = new_crypto_link
                changed = True
        elif new_url and subscription.subscription_crypto_link:
            subscription.subscription_crypto_link = None
            changed = True

        # Always stamp to protect from sync overwrite
        self._stamp_webhook_update(subscription)
        if changed:
            subscription.updated_at = datetime.now(UTC)
            logger.info(
                'Webhook: subscription credentials revoked/updated for user',
                subscription_id=subscription.id,
                user_id=user.id,
            )
        await db.commit()

        await self._notify_user(
            user, 'WEBHOOK_SUB_REVOKED', reply_markup=self._get_connect_keyboard(user), subscription=subscription
        )

    async def _handle_user_created(
        self, db: AsyncSession, user: User, subscription: Subscription | None, data: dict
    ) -> None:
        logger.info('Webhook: user created externally in panel', user_id=user.id, data=data.get('uuid'))

    async def _handle_expires_in_72h(
        self, db: AsyncSession, user: User, subscription: Subscription | None, data: dict
    ) -> None:
        if not subscription:
            logger.info('Webhook expires_72h: подписка не найдена в БД, пропуск', user_id=user.id)
            return
        await self._notify_user(
            user,
            'WEBHOOK_SUB_EXPIRES_72H',
            reply_markup=self._get_renew_keyboard(user, subscription.id),
            subscription=subscription,
        )

    async def _handle_expires_in_48h(
        self, db: AsyncSession, user: User, subscription: Subscription | None, data: dict
    ) -> None:
        if not subscription:
            logger.info('Webhook expires_48h: подписка не найдена в БД, пропуск', user_id=user.id)
            return
        await self._notify_user(
            user,
            'WEBHOOK_SUB_EXPIRES_48H',
            reply_markup=self._get_renew_keyboard(user, subscription.id),
            subscription=subscription,
        )

    async def _handle_expires_in_24h(
        self, db: AsyncSession, user: User, subscription: Subscription | None, data: dict
    ) -> None:
        if not subscription:
            logger.info('Webhook expires_24h: подписка не найдена в БД, пропуск', user_id=user.id)
            return
        await self._notify_user(
            user,
            'WEBHOOK_SUB_EXPIRES_24H',
            reply_markup=self._get_renew_keyboard(user, subscription.id),
            subscription=subscription,
        )

    async def _handle_expired_24h_ago(
        self, db: AsyncSession, user: User, subscription: Subscription | None, data: dict
    ) -> None:
        if not subscription:
            logger.info('Webhook expired_24h_ago: подписка не найдена в БД, пропуск', user_id=user.id)
            return
        await self._notify_user(
            user,
            'WEBHOOK_SUB_EXPIRED_24H_AGO',
            reply_markup=self._get_renew_keyboard(user, subscription.id),
            subscription=subscription,
        )

    async def _handle_user_expiration(
        self, db: AsyncSession, user: User, subscription: Subscription | None, data: dict
    ) -> None:
        """Remnawave 2.8.0: единое событие user.expiration (заменило 4 старых).

        ``meta.expiration`` — знаковые часы относительно истечения подписки
        (отрицательное = за |N| ч ДО, положительное = через N ч ПОСЛЕ). Канонические
        значения [-72, -48, -24, 24] маппятся на прежние сообщения 1:1; нестандартные
        значения из EXPIRATION_NOTIFICATIONS получают ближайшее по смыслу сообщение.
        """
        if not subscription:
            logger.info('Webhook user.expiration: подписка не найдена в БД, пропуск', user_id=user.id)
            return

        # Ресивер кладёт envelope-meta вебхука в data['_meta'] (см.
        # remnawave_webhook.py: «Inject meta into data ... via data.get('_meta')»),
        # ровно как читает сосед _handle_user_not_connected. НЕ 'meta'.
        meta = data.get('_meta') if isinstance(data.get('_meta'), dict) else {}
        raw = meta.get('expiration', data.get('expiration'))
        try:
            hours = int(raw)
        except (TypeError, ValueError):
            logger.warning('Webhook user.expiration: некорректное meta.expiration', user_id=user.id, raw=raw)
            return

        text_key = _EXPIRATION_HOURS_TO_TEXT_KEY.get(hours)
        if text_key is None:
            # Нестандартный EXPIRATION_NOTIFICATIONS: отрицательное → ближайшее «до
            # истечения», положительное → «истекла» (другого «после»-сообщения нет).
            if hours < 0:
                nearest = min((-72, -48, -24), key=lambda h: abs(h - hours))
                text_key = _EXPIRATION_HOURS_TO_TEXT_KEY[nearest]
            else:
                text_key = 'WEBHOOK_SUB_EXPIRED_24H_AGO'
            logger.info(
                'Webhook user.expiration: нестандартное значение, выбрано ближайшее сообщение',
                user_id=user.id,
                hours=hours,
                text_key=text_key,
            )

        await self._notify_user(
            user,
            text_key,
            reply_markup=self._get_renew_keyboard(user, subscription.id),
            subscription=subscription,
        )

    async def _handle_first_connected(
        self, db: AsyncSession, user: User, subscription: Subscription | None, data: dict
    ) -> None:
        logger.info('Webhook: user first VPN connection', user_id=user.id)
        await self._notify_user(
            user,
            'WEBHOOK_SUB_FIRST_CONNECTED',
            reply_markup=self._get_subscription_keyboard(user),
            subscription=subscription,
        )

    async def _handle_bandwidth_threshold(
        self, db: AsyncSession, user: User, subscription: Subscription | None, data: dict
    ) -> None:
        # Respect user notification preferences
        from app.utils.notification_prefs import is_traffic_warning_enabled

        if not is_traffic_warning_enabled(user):
            logger.debug('Traffic warning disabled by user prefs', user_id=user.id)
            return

        # Extract threshold percentage from meta or data
        percent = data.get('thresholdPercent') or data.get('threshold', '')
        if not percent:
            # Envelope-meta живёт в data['_meta'] (ресивер), не в 'meta'.
            meta = data.get('_meta', {})
            if isinstance(meta, dict):
                percent = meta.get('thresholdPercent', '80')

        # Sanitize to numeric value only (prevent format string injection)
        percent_str = re.sub(r'[^\d.]', '', str(percent)) or '80'

        await self._notify_user(
            user,
            'WEBHOOK_SUB_BANDWIDTH_THRESHOLD',
            reply_markup=self._get_traffic_keyboard(user),
            format_kwargs={'percent': percent_str},
            subscription=subscription,
        )

    async def _handle_user_not_connected(
        self, db: AsyncSession, user: User, subscription: Subscription | None, data: dict
    ) -> None:
        meta = data.get('_meta') or {}
        hours = meta.get('notConnectedAfterHours')
        logger.info(
            'Webhook: user has not connected to VPN',
            user_id=user.id,
            not_connected_after_hours=hours,
        )
        format_kwargs: dict[str, Any] = {}
        if hours is not None:
            format_kwargs['hours'] = str(hours)
        await self._notify_user(
            user,
            'WEBHOOK_USER_NOT_CONNECTED',
            reply_markup=self._get_connect_keyboard(user),
            format_kwargs=format_kwargs if format_kwargs else None,
            subscription=subscription,
        )

    # ------------------------------------------------------------------
    # Device event handlers (user_hwid_devices scope)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_device_name(data: dict) -> str:
        """Extract device name from webhook payload.

        RemnaWave sends device info in data['hwidUserDevice'] nested object.
        Builds a composite name: "tag (platform)" or just "platform" or hwid short.
        """
        device_obj = data.get('hwidUserDevice')
        if not isinstance(device_obj, dict):
            # Fallback: top-level fields
            raw = data.get('deviceName') or data.get('tag') or data.get('hwid') or ''
            return html.escape(str(raw)) if raw else ''

        tag = (device_obj.get('tag') or device_obj.get('deviceName') or device_obj.get('name') or '').strip()
        platform = (device_obj.get('platform') or '').strip()
        hwid = (device_obj.get('hwid') or '').strip()

        if tag and platform:
            return html.escape(f'{tag} ({platform})')
        if tag:
            return html.escape(tag)
        if platform and hwid:
            # Show platform + short hwid suffix for identification
            hwid_short = hwid[:8] if len(hwid) > 8 else hwid
            return html.escape(f'{platform} ({hwid_short})')
        if platform:
            return html.escape(platform)
        if hwid:
            hwid_short = hwid[:12] if len(hwid) > 12 else hwid
            return html.escape(hwid_short)
        return ''

    async def _handle_device_added(
        self, db: AsyncSession, user: User, subscription: Subscription | None, data: dict
    ) -> None:
        device_name = self._extract_device_name(data)
        logger.info('Webhook: device added for user', user_id=user.id, device_name=device_name or '(empty)')
        await self._notify_user(
            user,
            'WEBHOOK_DEVICE_ADDED',
            reply_markup=self._get_subscription_keyboard(user),
            format_kwargs={'device': device_name or '—'},
            subscription=subscription,
        )

    async def _handle_device_deleted(
        self, db: AsyncSession, user: User, subscription: Subscription | None, data: dict
    ) -> None:
        device_name = self._extract_device_name(data)
        logger.info('Webhook: device deleted for user', user_id=user.id, device_name=device_name or '(empty)')
        await self._notify_user(
            user,
            'WEBHOOK_DEVICE_DELETED',
            reply_markup=self._get_subscription_keyboard(user),
            format_kwargs={'device': device_name or '—'},
            subscription=subscription,
        )

    async def _handle_torrent_detected(
        self, db: AsyncSession, user: User, subscription: Subscription | None, data: dict
    ) -> None:
        logger.info('Webhook: torrent detected for user', user_id=user.id)
        await self._notify_user(
            user,
            'WEBHOOK_TORRENT_DETECTED',
            reply_markup=self._get_subscription_keyboard(user),
            subscription=subscription,
        )
