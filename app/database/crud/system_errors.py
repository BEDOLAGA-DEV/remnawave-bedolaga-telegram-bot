"""Выборки по журналу системных ошибок (``system_error_events``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import SystemErrorEvent


# Статусы, означающие «админ об этом не узнал»
UNDELIVERED_STATUSES = ('pending', 'failed')


async def list_error_events(
    db: AsyncSession,
    *,
    level: str | None = None,
    delivery_status: str | None = None,
    logger_name: str | None = None,
    search: str | None = None,
    undelivered_only: bool = False,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[SystemErrorEvent], int]:
    """Постранично отдать события с фильтрами. Возвращает (записи, всего)."""
    conditions = []

    if level:
        conditions.append(SystemErrorEvent.level == level)
    if delivery_status:
        conditions.append(SystemErrorEvent.delivery_status == delivery_status)
    if undelivered_only:
        conditions.append(SystemErrorEvent.delivery_status.in_(UNDELIVERED_STATUSES))
    if logger_name:
        conditions.append(SystemErrorEvent.logger_name.ilike(f'%{logger_name}%'))
    if search:
        pattern = f'%{search}%'
        conditions.append(
            or_(
                SystemErrorEvent.event.ilike(pattern),
                SystemErrorEvent.error_type.ilike(pattern),
                SystemErrorEvent.logger_name.ilike(pattern),
            )
        )
    if date_from:
        conditions.append(SystemErrorEvent.created_at >= date_from)
    if date_to:
        conditions.append(SystemErrorEvent.created_at <= date_to)

    count_query = select(func.count()).select_from(SystemErrorEvent)
    query = select(SystemErrorEvent)
    for condition in conditions:
        count_query = count_query.where(condition)
        query = query.where(condition)

    total = (await db.execute(count_query)).scalar() or 0

    query = query.order_by(SystemErrorEvent.created_at.desc()).limit(limit).offset(offset)
    items = list((await db.execute(query)).scalars().all())

    return items, total


async def get_error_event(db: AsyncSession, event_id: int) -> SystemErrorEvent | None:
    result = await db.execute(select(SystemErrorEvent).where(SystemErrorEvent.id == event_id))
    return result.scalar_one_or_none()


async def get_error_summary(db: AsyncSession) -> dict[str, Any]:
    """Сводка для бейджа и шапки страницы."""
    now = datetime.now(tz=UTC)
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)

    undelivered_total = (
        await db.execute(
            select(func.count())
            .select_from(SystemErrorEvent)
            .where(SystemErrorEvent.delivery_status.in_(UNDELIVERED_STATUSES))
        )
    ).scalar() or 0

    last_24h = (
        await db.execute(
            select(func.count()).select_from(SystemErrorEvent).where(SystemErrorEvent.created_at >= day_ago)
        )
    ).scalar() or 0

    last_7d = (
        await db.execute(
            select(func.count()).select_from(SystemErrorEvent).where(SystemErrorEvent.created_at >= week_ago)
        )
    ).scalar() or 0

    by_status_rows = (
        await db.execute(
            select(SystemErrorEvent.delivery_status, func.count())
            .where(SystemErrorEvent.created_at >= week_ago)
            .group_by(SystemErrorEvent.delivery_status)
        )
    ).all()

    top_rows = (
        await db.execute(
            select(SystemErrorEvent.error_type, SystemErrorEvent.event, func.count().label('cnt'))
            .where(SystemErrorEvent.created_at >= week_ago)
            .group_by(SystemErrorEvent.error_type, SystemErrorEvent.event)
            .order_by(func.count().desc())
            .limit(10)
        )
    ).all()

    return {
        'undelivered_total': undelivered_total,
        'last_24h': last_24h,
        'last_7d': last_7d,
        'by_status_7d': {status or 'unknown': count for status, count in by_status_rows},
        'top_errors_7d': [
            {'error_type': error_type, 'event': event, 'count': count} for error_type, event, count in top_rows
        ],
    }


async def mark_delivery_result(
    db: AsyncSession,
    event: SystemErrorEvent,
    *,
    delivered: bool,
    error: str | None = None,
) -> SystemErrorEvent:
    """Записать исход ручной повторной доставки."""
    event.delivery_attempts = (event.delivery_attempts or 0) + 1
    event.last_attempt_at = datetime.now(tz=UTC)
    if delivered:
        event.delivery_status = 'sent'
        event.delivered_at = datetime.now(tz=UTC)
        event.delivery_error = None
    else:
        event.delivery_status = 'failed'
        event.delivery_error = (error or '')[:1000] or None
    await db.commit()
    await db.refresh(event)
    return event
