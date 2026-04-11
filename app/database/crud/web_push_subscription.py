"""CRUD for WebPushSubscription (browser Web Push API)."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import WebPushSubscription


logger = structlog.get_logger(__name__)


async def upsert_subscription(
    db: AsyncSession,
    *,
    user_id: int,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str | None = None,
) -> WebPushSubscription:
    """Create or reactivate a web push subscription."""
    result = await db.execute(
        select(WebPushSubscription).where(
            and_(
                WebPushSubscription.user_id == user_id,
                WebPushSubscription.endpoint == endpoint,
            )
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.p256dh = p256dh
        existing.auth = auth
        existing.user_agent = user_agent or existing.user_agent
        existing.is_active = True
        await db.commit()
        await db.refresh(existing)
        return existing

    subscription = WebPushSubscription(
        user_id=user_id,
        endpoint=endpoint,
        p256dh=p256dh,
        auth=auth,
        user_agent=user_agent,
        is_active=True,
    )
    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)
    return subscription


async def deactivate_by_endpoint(
    db: AsyncSession,
    user_id: int,
    endpoint: str,
) -> bool:
    """Deactivate a subscription by its endpoint. Returns True if any row updated."""
    result = await db.execute(
        update(WebPushSubscription)
        .where(
            and_(
                WebPushSubscription.user_id == user_id,
                WebPushSubscription.endpoint == endpoint,
            )
        )
        .values(is_active=False)
    )
    await db.commit()
    return result.rowcount > 0


async def get_active_by_user(db: AsyncSession, user_id: int) -> list[WebPushSubscription]:
    """Get all active push subscriptions for a user."""
    result = await db.execute(
        select(WebPushSubscription).where(
            and_(
                WebPushSubscription.user_id == user_id,
                WebPushSubscription.is_active.is_(True),
            )
        )
    )
    return list(result.scalars().all())


async def get_all_active(db: AsyncSession) -> list[WebPushSubscription]:
    """Get all active push subscriptions (for global broadcasts)."""
    result = await db.execute(
        select(WebPushSubscription).where(WebPushSubscription.is_active.is_(True))
    )
    return list(result.scalars().all())


async def mark_last_used(db: AsyncSession, subscription_id: int) -> None:
    """Update last_used_at timestamp after successful push delivery."""
    await db.execute(
        update(WebPushSubscription)
        .where(WebPushSubscription.id == subscription_id)
        .values(last_used_at=datetime.now(UTC))
    )
    await db.commit()


async def deactivate_subscription(db: AsyncSession, subscription_id: int) -> None:
    """Deactivate a subscription by ID (used when push returns 404/410)."""
    await db.execute(
        update(WebPushSubscription)
        .where(WebPushSubscription.id == subscription_id)
        .values(is_active=False)
    )
    await db.commit()
