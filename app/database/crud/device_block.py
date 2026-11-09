"""CRUD for `device_blocks` — hard HWID device blocking.

Blocks live in our DB (single source of truth) and are pushed to the
hwid-blocker middleware as a full state snapshot. A blocked device receives
an empty profile + announce message from the middleware and cannot
reconnect until the row is deleted here.

Scope is per-(user, hwid); admin bans use user_id=NULL (Postgres unique
constraints treat NULLs as distinct, so several global blocks coexist).
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import DeviceBlock


logger = structlog.get_logger(__name__)

HWID_MAX_LENGTH: int = 64


def normalize_hwid(value: str | None) -> str:
    """Strip + collapse whitespace + cap length. Returns '' for empty input."""
    if value is None:
        return ''
    collapsed = ' '.join(value.split())
    return collapsed[:HWID_MAX_LENGTH]


async def get_block_by_hwid(db: AsyncSession, hwid: str, user_id: int | None = None) -> DeviceBlock | None:
    """Return the block for (user_id, hwid) or None.

    Pass user_id=None to search globally (any owner / admin ban).
    """
    stmt = select(DeviceBlock).where(DeviceBlock.hwid == hwid)
    if user_id is not None:
        stmt = stmt.where(DeviceBlock.user_id == user_id)
    result = await db.execute(stmt)
    return result.scalars().first()


async def get_blocks_for_user(db: AsyncSession, user_id: int) -> list[DeviceBlock]:
    """All blocks for a user, most recently banned first."""
    result = await db.execute(
        select(DeviceBlock)
        .where(DeviceBlock.user_id == user_id)
        .order_by(DeviceBlock.banned_at.desc(), DeviceBlock.id.desc())
    )
    return list(result.scalars().all())


async def get_all_blocks(db: AsyncSession) -> list[DeviceBlock]:
    """Every block (all users + admin bans), most recently banned first."""
    result = await db.execute(
        select(DeviceBlock).order_by(DeviceBlock.banned_at.desc(), DeviceBlock.id.desc())
    )
    return list(result.scalars().all())


async def upsert_block(
    db: AsyncSession,
    user_id: int | None,
    hwid: str,
    *,
    remnawave_uuid: str | None = None,
    short_uuid: str | None = None,
    device_label: str | None = None,
    platform: str | None = None,
    message: str | None = None,
    banned_by: str = 'user',
    commit: bool = True,
) -> DeviceBlock:
    """Insert or re-ban a device. Idempotent per (user_id, hwid, short_uuid).

    `short_uuid` (the Remnawave subscription key) scopes the ban to one
    subscription; pass None for a global ban. Re-banning refreshes the
    label/platform/message and bumps banned_at so the middleware etag
    (versioned) changes and stale client caches drop.
    """
    normalized = normalize_hwid(hwid)
    if not normalized:
        raise ValueError('hwid is required')

    now = datetime.now(timezone.utc)
    values = dict(
        user_id=user_id,
        hwid=normalized,
        remnawave_uuid=remnawave_uuid,
        short_uuid=short_uuid,
        device_label=device_label,
        platform=platform,
        message=message,
        banned_by=banned_by,
        banned_at=now,
        updated_at=now,
    )
    stmt = (
        pg_insert(DeviceBlock)
        .values(**values)
        .on_conflict_do_update(
            index_elements=['user_id', 'hwid', 'short_uuid'],
            set_={
                'remnawave_uuid': remnawave_uuid,
                'device_label': device_label,
                'platform': platform,
                'message': message,
                'banned_by': banned_by,
                'banned_at': now,
                'updated_at': now,
            },
        )
        .returning(DeviceBlock.id)
    )
    result = await db.execute(stmt)
    block_id = result.scalar_one()
    if commit:
        await db.commit()
    logger.info(
        'Device block upserted',
        user_id=user_id,
        hwid_prefix=normalized[:8],
        banned_by=banned_by,
        block_id=block_id,
    )

    result = await db.execute(select(DeviceBlock).where(DeviceBlock.id == block_id))
    return result.scalars().one()


async def delete_block(db: AsyncSession, block_id: int, *, commit: bool = True) -> bool:
    """Remove a block by primary key. Returns True if a row was removed."""
    stmt = sa_delete(DeviceBlock).where(DeviceBlock.id == block_id).returning(DeviceBlock.id)
    result = await db.execute(stmt)
    deleted = result.scalar_one_or_none() is not None
    if commit:
        await db.commit()
    if deleted:
        logger.info('Device block deleted', block_id=block_id)
    return deleted


async def delete_block_for_user(db: AsyncSession, user_id: int, hwid: str, *, commit: bool = True) -> bool:
    """Remove the block for (user_id, hwid) without a prior lookup."""
    normalized = normalize_hwid(hwid)
    if not normalized:
        raise ValueError('hwid is required')
    stmt = (
        sa_delete(DeviceBlock)
        .where(DeviceBlock.user_id == user_id, DeviceBlock.hwid == normalized)
        .returning(DeviceBlock.id)
    )
    result = await db.execute(stmt)
    deleted = result.scalar_one_or_none() is not None
    if commit:
        await db.commit()
    return deleted


async def touch_last_seen(db: AsyncSession, hwid: str, *, commit: bool = False) -> None:
    """Mark that a blocked device hit the middleware (audit only)."""
    normalized = normalize_hwid(hwid)
    if not normalized:
        return
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(DeviceBlock)
        .where(DeviceBlock.hwid == normalized)
        .order_by(DeviceBlock.banned_at.desc())
        .limit(1)
    )
    block = result.scalars().first()
    if block is None:
        return
    block.last_seen_at = now
    await db.flush()
    if commit:
        await db.commit()
