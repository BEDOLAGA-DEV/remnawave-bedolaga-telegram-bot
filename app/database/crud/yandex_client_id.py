"""CRUD operations for yandex_client_id_map table."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import YandexClientIdMap


logger = structlog.get_logger(__name__)


async def upsert_cid(
    db: AsyncSession,
    user_id: int,
    cid: str,
    source: str = 'web',
    counter_id: str | None = None,
) -> YandexClientIdMap | None:
    """Insert Yandex ClientID for a user. First CID is preserved — never overwritten."""
    stmt = (
        pg_insert(YandexClientIdMap)
        .values(user_id=user_id, yandex_cid=cid, source=source, counter_id=counter_id)
        .on_conflict_do_nothing(index_elements=['user_id'])
        .returning(YandexClientIdMap)
    )

    result = await db.execute(stmt)
    await db.flush()
    row = result.scalar_one_or_none()
    if row:
        return row
    # Already existed — return existing record (CID not overwritten)
    return await get_cid(db, user_id)


async def get_cid(db: AsyncSession, user_id: int) -> YandexClientIdMap | None:
    """Get Yandex ClientID mapping for a user."""
    result = await db.execute(select(YandexClientIdMap).where(YandexClientIdMap.user_id == user_id))
    return result.scalar_one_or_none()


async def mark_registration_sent(db: AsyncSession, user_id: int) -> None:
    """Mark registration event as sent for a user."""
    await db.execute(
        update(YandexClientIdMap)
        .where(YandexClientIdMap.user_id == user_id)
        .values(registration_sent=True, updated_at=datetime.now(UTC))
    )
    await db.flush()


async def mark_trial_sent(db: AsyncSession, user_id: int) -> None:
    """Mark trial event as sent for a user."""
    await db.execute(
        update(YandexClientIdMap)
        .where(YandexClientIdMap.user_id == user_id)
        .values(trial_sent=True, updated_at=datetime.now(UTC))
    )
    await db.flush()
