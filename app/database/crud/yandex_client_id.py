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
) -> None:
    """Insert or update Yandex ClientID for a user."""
    now = datetime.now(UTC)
    values: dict = {
        'user_id': user_id,
        'yandex_cid': cid,
        'source': source,
        'updated_at': now,
    }
    if counter_id:
        values['counter_id'] = counter_id

    update_set: dict = {
        'yandex_cid': cid,
        'source': source,
        'updated_at': now,
    }
    if counter_id:
        update_set['counter_id'] = counter_id

    stmt = (
        pg_insert(YandexClientIdMap)
        .values(**values)
        .on_conflict_do_update(
            index_elements=['user_id'],
            set_=update_set,
        )
    )
    await db.execute(stmt)
    await db.flush()


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
