from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import SubscriptionEvent


async def create_subscription_event(
    db: AsyncSession,
    *,
    user_id: int,
    event_type: str,
    subscription_id: int | None = None,
    transaction_id: int | None = None,
    amount_kopeks: int | None = None,
    currency: str | None = None,
    message: str | None = None,
    occurred_at: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> SubscriptionEvent:
    event = SubscriptionEvent(
        user_id=user_id,
        event_type=event_type,
        subscription_id=subscription_id,
        transaction_id=transaction_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        message=message,
        occurred_at=occurred_at or datetime.now(UTC),
        extra=extra or None,
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return event


async def list_subscription_events(
    db: AsyncSession,
    *,
    limit: int,
    offset: int,
    event_types: Iterable[str] | None = None,
    user_id: int | None = None,
) -> tuple[list[SubscriptionEvent], int]:
    base_query = select(SubscriptionEvent)
    filters = []

    if event_types:
        filters.append(SubscriptionEvent.event_type.in_(set(event_types)))
    if user_id:
        filters.append(SubscriptionEvent.user_id == user_id)

    if filters:
        base_query = base_query.where(and_(*filters))

    # По колонке, а не func.count(): иначе без фильтров FROM теряется и total = 1.
    # См. tests/webapi/test_list_total_counts_rows.py.
    total_query = base_query.with_only_columns(func.count(SubscriptionEvent.id)).order_by(None)
    total = await db.scalar(total_query) or 0

    result = await db.execute(
        base_query.options(selectinload(SubscriptionEvent.user))
        .order_by(SubscriptionEvent.occurred_at.desc())
        .offset(offset)
        .limit(limit)
    )

    return result.scalars().all(), int(total)
