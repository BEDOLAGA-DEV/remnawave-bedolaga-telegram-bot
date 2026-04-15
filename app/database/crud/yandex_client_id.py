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
    subid: str | None = None,
) -> YandexClientIdMap:
    """Insert or update Yandex ClientID for a user (race-safe via ON CONFLICT)."""
    now = datetime.now(UTC)
    values = {
        'yandex_cid': cid,
        'source': source,
        'updated_at': now,
    }
    if counter_id:
        values['counter_id'] = counter_id
    if subid:
        values['subid'] = subid

    stmt = (
        pg_insert(YandexClientIdMap)
        .values(user_id=user_id, yandex_cid=cid, source=source, counter_id=counter_id, subid=subid)
        .on_conflict_do_update(index_elements=['user_id'], set_=values)
        .returning(YandexClientIdMap)
    )

    result = await db.execute(stmt)
    await db.flush()
    return result.scalar_one()


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


async def upsert_subid(
    db: AsyncSession,
    user_id: int,
    subid: str,
    source: str = 'web',
) -> None:
    """Save subid for a user. Updates existing record or creates with placeholder CID."""
    if not subid or len(subid) > 255:
        return
    now = datetime.now(UTC)
    # Try update first (don't create empty CID records)
    result = await db.execute(
        update(YandexClientIdMap).where(YandexClientIdMap.user_id == user_id).values(subid=subid, updated_at=now)
    )
    if result.rowcount == 0:
        # No existing record — create with placeholder
        stmt = (
            pg_insert(YandexClientIdMap)
            .values(user_id=user_id, yandex_cid='_subid_only', source=source, subid=subid)
            .on_conflict_do_update(
                index_elements=['user_id'],
                set_={'subid': subid, 'updated_at': now},
            )
        )
        await db.execute(stmt)
    await db.flush()
    logger.info('Subid saved', user_id=user_id, subid=subid, source=source)


async def get_subid(db: AsyncSession, user_id: int) -> str | None:
    """Get subid for a user."""
    result = await db.execute(select(YandexClientIdMap.subid).where(YandexClientIdMap.user_id == user_id))
    return result.scalar_one_or_none()
