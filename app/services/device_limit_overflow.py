"""Подписки, у которых в панели зарегистрировано больше HWID-устройств, чем разрешает лимит.

Лимит панель применяет к новым устройствам: уже зарегистрированные остаются за
пользователем и продолжают обновлять подписку, когда лимит опускается ниже их
числа. Опускают его штатные действия — смена тарифа (лимит сбрасывается до базы
нового тарифа), массовое действие, правка админом, — и бот этого расхождения не
видел: пользователь видит «10 из 8», админ узнаёт о нём только из обращения.

Проверка раз в сутки сравнивает число устройств пользователя панели с лимитом,
который бот отправляет в панель, и сообщает админам о новых и изменившихся
случаях. Какой случай уже показан, помнит кэш (``DEVICE_OVERFLOW_REPORT_TTL``):
пока число устройств и лимит прежние, повтор не раньше чем через этот срок.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import Subscription, SubscriptionStatus, User
from app.utils.cache import cache
from app.utils.subscription_utils import resolve_hwid_device_limit


logger = structlog.get_logger(__name__)

DEVICE_OVERFLOW_ACTIVE_DAYS = 30
DEVICE_OVERFLOW_REPORT_TTL = timedelta(days=30)
_CACHE_KEY = 'device_limit_overflow:{subscription_id}'
_CHECKED_STATUSES = (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value)


@dataclass(frozen=True)
class DeviceOverflow:
    subscription: Subscription
    device_limit: int
    devices: int
    active_devices: int

    @property
    def signature(self) -> str:
        return f'{self.devices}:{self.device_limit}'


def _panel_user_id(subscription: Subscription, user: User) -> int | None:
    """Тот же выбор, что у экранов устройств: в мультитарифе у каждой подписки свой пользователь панели."""
    if settings.is_multi_tariff_enabled():
        return subscription.remnawave_id
    return user.remnawave_id or subscription.remnawave_id


def _parse_panel_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def find_device_overflows(
    subscriptions: Iterable[Subscription],
    devices: Iterable[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> list[DeviceOverflow]:
    """Подписки, где устройств в панели больше лимита; без лимита (0 / пусто) не проверяются."""
    moment = now or datetime.now(UTC)
    active_after = moment - timedelta(days=DEVICE_OVERFLOW_ACTIVE_DAYS)

    total: dict[int, int] = {}
    active: dict[int, int] = {}
    for device in devices:
        panel_user_id = device.get('userId')
        if not isinstance(panel_user_id, int):
            continue
        total[panel_user_id] = total.get(panel_user_id, 0) + 1
        seen_at = _parse_panel_datetime(device.get('updatedAt'))
        if seen_at is not None and seen_at >= active_after:
            active[panel_user_id] = active.get(panel_user_id, 0) + 1

    overflows: list[DeviceOverflow] = []
    for subscription in subscriptions:
        user = subscription.user
        if user is None:
            continue
        panel_user_id = _panel_user_id(subscription, user)
        if panel_user_id is None:
            continue
        limit = resolve_hwid_device_limit(subscription)
        count = total.get(panel_user_id, 0)
        if limit is None or count <= limit:
            continue
        overflows.append(
            DeviceOverflow(
                subscription=subscription,
                device_limit=limit,
                devices=count,
                active_devices=active.get(panel_user_id, 0),
            )
        )

    overflows.sort(key=lambda item: (item.device_limit - item.devices, item.subscription.id))
    return overflows


async def load_checked_subscriptions(db: AsyncSession) -> list[Subscription]:
    result = await db.execute(
        select(Subscription)
        .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
        .where(Subscription.status.in_(_CHECKED_STATUSES))
    )
    return list(result.scalars().all())


async def select_unreported(overflows: list[DeviceOverflow]) -> list[DeviceOverflow]:
    """Новые случаи и те, у которых сменилось число устройств или лимит."""
    fresh: list[DeviceOverflow] = []
    for overflow in overflows:
        reported = await cache.get(_CACHE_KEY.format(subscription_id=overflow.subscription.id))
        if reported != overflow.signature:
            fresh.append(overflow)
    return fresh


async def mark_reported(overflows: list[DeviceOverflow]) -> None:
    for overflow in overflows:
        await cache.set(
            _CACHE_KEY.format(subscription_id=overflow.subscription.id),
            overflow.signature,
            expire=DEVICE_OVERFLOW_REPORT_TTL,
        )


async def check_device_limit_overflows(db: AsyncSession, bot: Any) -> int:
    """Одна проверка: найти превышения, отправить новые админам. Возвращает число отправленных случаев."""
    if not bot or not settings.is_admin_notifications_enabled():
        return 0
    if not settings.ADMIN_NOTIFICATIONS_ADDONS_ENABLED:
        return 0

    from app.services.admin_notification_service import AdminNotificationService
    from app.services.remnawave_service import RemnaWaveService

    service = RemnaWaveService()
    if not service.is_configured:
        return 0

    async with service.get_api_client() as api:
        devices_data = await api.get_all_hwid_devices()

    overflows = find_device_overflows(await load_checked_subscriptions(db), devices_data.get('devices', []))
    fresh = await select_unreported(overflows)
    logger.info('Проверка превышения лимита устройств', overflows=len(overflows), unreported=len(fresh))
    if not fresh:
        return 0

    sent = await AdminNotificationService(bot).send_device_limit_overflow_notification(fresh, total=len(overflows))
    if not sent:
        return 0
    await mark_reported(fresh)
    return len(fresh)
