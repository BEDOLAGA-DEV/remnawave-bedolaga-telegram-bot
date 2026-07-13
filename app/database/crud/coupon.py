"""CRUD helpers for wholesale coupon batches and their one-time coupons."""

import secrets
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Coupon, CouponBatch, CouponStatus


def generate_coupon_token() -> str:
    """128-bit hex token: ``coupon_`` + 32 chars fits Telegram's 64-char start param."""
    return secrets.token_hex(16)


async def create_coupon_batch(
    db: AsyncSession,
    *,
    name: str,
    tariff_id: int,
    period_days: int,
    coupons_count: int,
    wholesale_price_kopeks: int = 0,
    valid_until: datetime | None = None,
    created_by: int | None = None,
) -> CouponBatch:
    batch = CouponBatch(
        name=name,
        tariff_id=tariff_id,
        period_days=period_days,
        coupons_total=coupons_count,
        wholesale_price_kopeks=wholesale_price_kopeks,
        valid_until=valid_until,
        created_by=created_by,
    )
    db.add(batch)
    await db.flush()

    coupons = [Coupon(batch_id=batch.id, token=generate_coupon_token()) for _ in range(coupons_count)]
    db.add_all(coupons)
    await db.commit()
    await db.refresh(batch)
    return batch


async def get_coupon_batch_by_id(db: AsyncSession, batch_id: int) -> CouponBatch | None:
    result = await db.execute(select(CouponBatch).where(CouponBatch.id == batch_id))
    return result.scalar_one_or_none()


async def get_coupon_batches(db: AsyncSession, *, offset: int = 0, limit: int = 10) -> list[CouponBatch]:
    result = await db.execute(select(CouponBatch).order_by(CouponBatch.id.desc()).offset(offset).limit(limit))
    return list(result.scalars().all())


async def get_coupon_batches_count(db: AsyncSession) -> int:
    result = await db.execute(select(func.count(CouponBatch.id)))
    return result.scalar() or 0


async def get_coupon_by_token(db: AsyncSession, token: str) -> Coupon | None:
    result = await db.execute(select(Coupon).where(Coupon.token == token))
    return result.scalars().first()


async def get_batch_coupon_tokens(db: AsyncSession, batch_id: int, *, status: str | None = None) -> list[str]:
    """Plain token strings (no entity hydration) — enough for the links export."""
    query = select(Coupon.token).where(Coupon.batch_id == batch_id)
    if status is not None:
        query = query.where(Coupon.status == status)
    result = await db.execute(query.order_by(Coupon.id))
    return list(result.scalars().all())


async def get_batch_status_counts(db: AsyncSession, batch_id: int) -> dict[str, int]:
    result = await db.execute(
        select(Coupon.status, func.count(Coupon.id)).where(Coupon.batch_id == batch_id).group_by(Coupon.status)
    )
    return dict(result.all())


async def get_status_counts_for_batches(db: AsyncSession, batch_ids: list[int]) -> dict[int, dict[str, int]]:
    """Status counts for many batches in one query (list views)."""
    if not batch_ids:
        return {}
    result = await db.execute(
        select(Coupon.batch_id, Coupon.status, func.count(Coupon.id))
        .where(Coupon.batch_id.in_(batch_ids))
        .group_by(Coupon.batch_id, Coupon.status)
    )
    counts: dict[int, dict[str, int]] = {}
    for batch_id, coupon_status, count in result.all():
        counts.setdefault(batch_id, {})[coupon_status] = count
    return counts


async def revoke_batch_coupons(db: AsyncSession, batch: CouponBatch) -> int:
    """Flip all still-active coupons of the batch to REVOKED. Returns how many were revoked.

    Safe against a concurrent redemption: the redeem path holds the coupon row
    FOR UPDATE and re-checks the status, so whichever transaction commits first
    wins and the other sees the new status.
    """
    result = await db.execute(
        update(Coupon)
        .where(Coupon.batch_id == batch.id, Coupon.status == CouponStatus.ACTIVE.value)
        .values(status=CouponStatus.REVOKED.value)
    )
    batch.is_revoked = True
    await db.commit()
    return result.rowcount or 0
