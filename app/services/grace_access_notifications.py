"""Уведомления о grace-доступе: админам в чат и человеку в бота.

Владелец (2026-09-14): «нужна уведа для админов, что чел получил грейс,
закончился грейс — иначе выдача втухлую». До этого grace выдавался и закрывался
молча: человек видел «VPN не работает», хотя работал Telegram, а админ узнавал
о выдаче только из таблицы сессий.

Рантайм зовёт ``announce_grace_event`` ПОСЛЕ коммита состояния сессии; здесь
ничего не решается и ничего не бросается — сбой доставки не должен трогать сам
grace. Ключи ``GRACE_ACCESS_NOTIFY_ADMINS`` / ``GRACE_ACCESS_NOTIFY_USER``
выключают каждую аудиторию отдельно.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import GraceAccessSessionModel, Subscription
from app.services.notification_types import NotificationType


logger = structlog.get_logger(__name__)

GraceEvent = Literal['granted', 'ended']
_GIB = 1024**3
#: О продлении человек уже получает своё уведомление; отзыв и конфликт — служебные
#: исходы, человеку о них сказать нечего. Ему сообщаем только «доступ закрылся».
_USER_ENDED_REASONS = frozenset({'timeout', 'drained'})


@dataclass(frozen=True, slots=True)
class _GraceDetails:
    reason: str
    grace_until: datetime
    hours: int
    quota_gb: float
    completion_reason: str | None
    last_error: str | None


async def announce_grace_event(bot: Any, subscription_id: int, event: GraceEvent) -> None:
    """Сообщить о выдаче или завершении grace. Никогда не бросает."""
    if bot is None:
        return
    if not (settings.GRACE_ACCESS_NOTIFY_ADMINS or settings.GRACE_ACCESS_NOTIFY_USER):
        return
    try:
        async with AsyncSessionLocal() as db:
            subscription = (
                await db.execute(
                    select(Subscription)
                    .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
                    .where(Subscription.id == subscription_id)
                )
            ).scalar_one_or_none()
            row = (
                await db.execute(
                    select(GraceAccessSessionModel)
                    .where(GraceAccessSessionModel.subscription_id == subscription_id)
                    .order_by(GraceAccessSessionModel.updated_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if subscription is None or subscription.user is None or row is None:
            logger.warning(
                'Grace event has no subscription or session to describe',
                subscription_id=subscription_id,
                grace_event=event,
            )
            return
        details = _details(row)
    except Exception:
        logger.exception('Grace event could not be loaded', subscription_id=subscription_id, grace_event=event)
        return

    if settings.GRACE_ACCESS_NOTIFY_ADMINS:
        try:
            await _notify_admins(bot, event, subscription, details)
        except Exception:
            logger.exception('Grace admin notification failed', subscription_id=subscription_id, grace_event=event)
    if settings.GRACE_ACCESS_NOTIFY_USER:
        try:
            await _notify_user(bot, event, subscription, details)
        except Exception:
            logger.exception('Grace user notification failed', subscription_id=subscription_id, grace_event=event)


def _details(row: GraceAccessSessionModel) -> _GraceDetails:
    overlay = row.overlay if isinstance(row.overlay, dict) else {}
    before = row.panel_before if isinstance(row.panel_before, dict) else {}
    limit = int(overlay.get('traffic_limit_bytes') or 0)
    used = int(before.get('used_traffic_bytes') or 0)
    quota_gb = max(0.0, (limit - used) / _GIB)
    grace_until = _as_utc(row.grace_until)
    started_at = _as_utc(row.started_at) if row.started_at else grace_until
    hours = (
        max(1, round((grace_until - started_at).total_seconds() / 3600))
        if row.started_at
        else int(settings.GRACE_ACCESS_DURATION_HOURS)
    )
    return _GraceDetails(
        reason=str(row.reason),
        grace_until=grace_until,
        hours=hours,
        quota_gb=round(quota_gb, 2),
        completion_reason=str(row.completion_reason) if row.completion_reason else None,
        last_error=str(row.last_error) if row.last_error else None,
    )


async def _notify_admins(bot: Any, event: GraceEvent, subscription: Subscription, details: _GraceDetails) -> None:
    from app.services.admin_notification_service import AdminNotificationService

    tariff = subscription.tariff
    await AdminNotificationService(bot).send_grace_access_notification(
        event=event,
        user=subscription.user,
        subscription=subscription,
        tariff_name=getattr(tariff, 'name', None) if tariff else None,
        reason=details.reason,
        grace_until=details.grace_until,
        hours=details.hours,
        quota_gb=details.quota_gb,
        allowed=html.escape(grace_allowed_services(), quote=False),
        completion_reason=details.completion_reason,
        last_error=details.last_error,
    )


async def _notify_user(bot: Any, event: GraceEvent, subscription: Subscription, details: _GraceDetails) -> None:
    from app.localization.texts import get_texts
    from app.services.notification_delivery_service import notification_delivery_service
    from app.utils.miniapp_buttons import build_subscription_extend_button
    from app.utils.timezone import format_local_datetime

    if event == 'ended' and details.completion_reason not in _USER_ENDED_REASONS:
        return

    user = subscription.user
    texts = get_texts(user.language)
    if event == 'granted':
        text_key = 'GRACE_ACCESS_GRANTED_LIMITED' if details.reason == 'limited' else 'GRACE_ACCESS_GRANTED_EXPIRED'
        notification_type = NotificationType.GRACE_ACCESS_GRANTED
    else:
        text_key = 'GRACE_ACCESS_ENDED'
        notification_type = NotificationType.GRACE_ACCESS_ENDED
    template = texts.get(text_key)
    if not template:
        logger.warning('Missing locale key for grace notification', text_key=text_key, language=user.language)
        return

    tariff = subscription.tariff
    tariff_name = str(getattr(tariff, 'name', '') or '') if tariff else ''
    # Всё, что попадает в HTML Telegram и письма, экранируется: фразу «что доступно»
    # и имя тарифа пишет оператор, а разметку сообщения — мы.
    allowed = html.escape(grace_allowed_services(), quote=False)
    context = {
        'tariff_label': f' «{html.escape(tariff_name, quote=False)}»'
        if settings.is_multi_tariff_enabled() and tariff_name
        else '',
        'allowed': allowed,
        'hours': details.hours,
        'traffic_gb': f'{details.quota_gb:g}',
        'until': format_local_datetime(details.grace_until, '%d.%m.%Y %H:%M'),
    }
    message = template.format(**context)
    # Письму — сырые значения: его шаблон экранирует сам, а редактор писем
    # подставляет их в свой текст.
    email_context = {
        'text_key': text_key,
        'allowed': grace_allowed_services(),
        'hours': details.hours,
        'traffic_gb': f'{details.quota_gb:g}',
        'until': context['until'],
        'reason': details.reason,
        'tariff_name': tariff_name,
    }
    renew = build_subscription_extend_button(texts.get('WEBHOOK_RENEW_BUTTON', 'Renew subscription'), subscription.id)
    close = InlineKeyboardButton(text=texts.get('WEBHOOK_CLOSE_BUTTON', '✖️ Закрыть'), callback_data='webhook:close')
    await notification_delivery_service.send_notification(
        user=user,
        notification_type=notification_type,
        context=email_context,
        bot=bot,
        telegram_message=message,
        telegram_markup=InlineKeyboardMarkup(inline_keyboard=[[renew], [close]]),
    )


def grace_allowed_services() -> str:
    """Что остаётся доступным во время grace — словами оператора; пусто = Telegram."""
    return (getattr(settings, 'GRACE_ACCESS_ALLOWED_SERVICES', '') or '').strip() or 'Telegram'


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
