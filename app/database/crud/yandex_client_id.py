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
    """Save subid for a user — first-writer-wins.

    If a subid is already attributed to the user it is kept intact; this blocks
    affiliate-revenue hijack via attacker-controlled deep-link `?start=foo_clk_<their_subid>`
    or repeated cabinet `POST /partner-click-id` calls overwriting the original.
    """
    if not subid or len(subid) > 255:
        return
    now = datetime.now(UTC)
    # Update only rows where subid IS NULL — never overwrite an existing attribution.
    result = await db.execute(
        update(YandexClientIdMap)
        .where(YandexClientIdMap.user_id == user_id, YandexClientIdMap.subid.is_(None))
        .values(subid=subid, updated_at=now)
    )
    if result.rowcount == 0:
        # Either no row yet, or row exists with a non-null subid. Insert-or-keep
        # via ON CONFLICT DO UPDATE that only writes subid when current value IS NULL.
        stmt = pg_insert(YandexClientIdMap).values(
            user_id=user_id,
            yandex_cid='_subid_only',
            source=source,
            subid=subid,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=['user_id'],
            set_={'subid': stmt.excluded.subid, 'updated_at': now},
            where=YandexClientIdMap.subid.is_(None),
        )
        await db.execute(stmt)
    await db.flush()
    logger.info(
        'subid_saved',
        user_id=user_id,
        subid_prefix=subid[:8],
        source=source,
    )


async def get_subid(db: AsyncSession, user_id: int) -> str | None:
    """Get subid for a user."""
    result = await db.execute(select(YandexClientIdMap.subid).where(YandexClientIdMap.user_id == user_id))
    return result.scalar_one_or_none()
