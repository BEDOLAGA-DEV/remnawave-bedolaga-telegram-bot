"""CRUD for UserNotification (persistent in-app notifications)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import UserNotification


logger = structlog.get_logger(__name__)


async def create_notification(
    db: AsyncSession,
    *,
    user_id: int,
    category: str,
    message: str,
    level: str = 'info',
    title: str | None = None,
    action_url: str | None = None,
    data: dict[str, Any] | None = None,
    commit: bool = True,
) -> UserNotification:
    """Create a single notification for a user."""
    notification = UserNotification(
        user_id=user_id,
        category=category,
        level=level,
        title=title,
        message=message,
        action_url=action_url,
        data=data or {},
    )
    db.add(notification)
    if commit:
        await db.commit()
        await db.refresh(notification)
    else:
        await db.flush()
    return notification


async def bulk_create_notifications(
    db: AsyncSession,
    *,
    user_ids: list[int],
    category: str,
    message: str,
    level: str = 'info',
    title: str | None = None,
    action_url: str | None = None,
    data: dict[str, Any] | None = None,
) -> list[UserNotification]:
    """Create notifications for many users at once (admin broadcast)."""
    notifications = [
        UserNotification(
            user_id=uid,
            category=category,
            level=level,
            title=title,
            message=message,
            action_url=action_url,
            data=data or {},
        )
        for uid in user_ids
    ]
    db.add_all(notifications)
    await db.commit()
    return notifications


async def get_user_notifications(
    db: AsyncSession,
    user_id: int,
    *,
    unread_only: bool = False,
    category: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[UserNotification], int]:
    """Get a paginated list of notifications for a user. Returns (items, total)."""
    conditions = [UserNotification.user_id == user_id]
    if unread_only:
        conditions.append(UserNotification.read_at.is_(None))
    if category:
        conditions.append(UserNotification.category == category)

    count_result = await db.execute(
        select(func.count(UserNotification.id)).where(and_(*conditions))
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(UserNotification)
        .where(and_(*conditions))
        .order_by(UserNotification.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), total


async def get_unread_count(db: AsyncSession, user_id: int) -> int:
    """Count user's unread notifications."""
    result = await db.execute(
        select(func.count(UserNotification.id)).where(
            and_(
                UserNotification.user_id == user_id,
                UserNotification.read_at.is_(None),
            )
        )
    )
    return int(result.scalar_one())


async def mark_as_read(db: AsyncSession, notification_id: int, user_id: int) -> bool:
    """Mark a single notification as read. Returns True if updated."""
    result = await db.execute(
        update(UserNotification)
        .where(
            and_(
                UserNotification.id == notification_id,
                UserNotification.user_id == user_id,
                UserNotification.read_at.is_(None),
            )
        )
        .values(read_at=datetime.now(UTC))
    )
    await db.commit()
    return result.rowcount > 0


async def mark_all_as_read(db: AsyncSession, user_id: int) -> int:
    """Mark all user's notifications as read. Returns count updated."""
    result = await db.execute(
        update(UserNotification)
        .where(
            and_(
                UserNotification.user_id == user_id,
                UserNotification.read_at.is_(None),
            )
        )
        .values(read_at=datetime.now(UTC))
    )
    await db.commit()
    return result.rowcount


async def delete_notification(db: AsyncSession, notification_id: int, user_id: int) -> bool:
    """Delete a user's notification. Returns True if deleted."""
    result = await db.execute(
        select(UserNotification).where(
            and_(
                UserNotification.id == notification_id,
                UserNotification.user_id == user_id,
            )
        )
    )
    notification = result.scalar_one_or_none()
    if not notification:
        return False
    await db.delete(notification)
    await db.commit()
    return True


async def check_recent_traffic_warning(
    db: AsyncSession,
    user_id: int,
    subscription_id: int,
    threshold_percent: int,
) -> bool:
    """Returns True if a traffic_warning for this subscription at this threshold
    was already sent recently (within the last 7 days).

    Used to deduplicate warnings within one billing period.
    """
    from datetime import timedelta

    cutoff = datetime.now(UTC) - timedelta(days=7)
    result = await db.execute(
        select(func.count(UserNotification.id)).where(
            and_(
                UserNotification.user_id == user_id,
                UserNotification.category == 'traffic_warning',
                UserNotification.created_at >= cutoff,
                UserNotification.data['subscription_id'].as_integer() == subscription_id,
                UserNotification.data['threshold_percent'].as_integer() == threshold_percent,
            )
        )
    )
    count = int(result.scalar_one() or 0)
    return count > 0
