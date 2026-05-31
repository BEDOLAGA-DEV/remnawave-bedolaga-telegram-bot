from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ReferralMilestone, UserReferralMilestoneClaim


logger = structlog.get_logger(__name__)

_REWARD_TYPES = ('balance', 'promo_group')


async def list_active(db: AsyncSession) -> list[ReferralMilestone]:
    result = await db.execute(
        select(ReferralMilestone)
        .where(ReferralMilestone.is_active == True)  # noqa: E712
        .order_by(ReferralMilestone.threshold.asc())
    )
    return list(result.scalars().all())


async def list_all(db: AsyncSession) -> list[ReferralMilestone]:
    result = await db.execute(select(ReferralMilestone).order_by(ReferralMilestone.threshold.asc()))
    return list(result.scalars().all())


async def get(db: AsyncSession, milestone_id: int) -> ReferralMilestone | None:
    result = await db.execute(select(ReferralMilestone).where(ReferralMilestone.id == milestone_id))
    return result.scalar_one_or_none()


async def create(db: AsyncSession, *, threshold: int, reward_type: str, reward_value: int,
                 title: dict | None = None, is_active: bool = True) -> ReferralMilestone:
    if reward_type not in _REWARD_TYPES:
        raise ValueError(f'reward_type must be one of {_REWARD_TYPES}')
    if threshold < 1:
        raise ValueError('threshold must be >= 1')
    if reward_value < 0:
        raise ValueError('reward_value must be >= 0')
    m = ReferralMilestone(
        threshold=threshold, reward_type=reward_type, reward_value=reward_value,
        title=title or {}, is_active=is_active,
    )
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


async def update_milestone(db: AsyncSession, milestone_id: int, **fields) -> ReferralMilestone | None:
    if 'reward_type' in fields and fields['reward_type'] not in _REWARD_TYPES:
        raise ValueError(f'reward_type must be one of {_REWARD_TYPES}')
    m = await get(db, milestone_id)
    if m is None:
        return None
    for k, v in fields.items():
        if hasattr(m, k):
            setattr(m, k, v)
    await db.commit()
    await db.refresh(m)
    return m


async def delete(db: AsyncSession, milestone_id: int) -> bool:
    m = await get(db, milestone_id)
    if m is None:
        return False
    await db.delete(m)
    await db.commit()
    return True


async def get_claimed_milestone_ids(db: AsyncSession, user_id: int) -> set[int]:
    result = await db.execute(
        select(UserReferralMilestoneClaim.milestone_id).where(UserReferralMilestoneClaim.user_id == user_id)
    )
    return {row[0] for row in result.all()}
