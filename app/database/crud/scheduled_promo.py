from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ScheduledPromo


logger = structlog.get_logger(__name__)


class ScheduledPromoCRUD:
    """CRUD operations for scheduled_promos table."""

    @staticmethod
    async def get_active_promos(db: AsyncSession) -> list[ScheduledPromo]:
        """Get promos where now() is between start_at and end_at and is_active."""
        now = datetime.now(UTC)
        result = await db.execute(
            select(ScheduledPromo).where(
                and_(
                    ScheduledPromo.is_active.is_(True),
                    ScheduledPromo.start_at <= now,
                    ScheduledPromo.end_at >= now,
                )
            ).order_by(ScheduledPromo.id)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_all_promos(db: AsyncSession) -> list[ScheduledPromo]:
        result = await db.execute(
            select(ScheduledPromo).order_by(ScheduledPromo.id.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def create_promo(
        db: AsyncSession,
        *,
        name: str,
        discount_percent: int,
        start_at: datetime,
        end_at: datetime,
        tariff_ids: list[int] | None = None,
        promo_text: str | None = None,
        created_by: int | None = None,
    ) -> ScheduledPromo:
        promo = ScheduledPromo(
            name=name,
            discount_percent=discount_percent,
            tariff_ids=tariff_ids or [],
            promo_text=promo_text,
            start_at=start_at,
            end_at=end_at,
            created_by=created_by,
        )
        db.add(promo)
        await db.flush()
        await db.refresh(promo)
        logger.info('Created scheduled promo', promo_id=promo.id, name=name, discount=discount_percent)
        return promo

    @staticmethod
    async def delete_promo(db: AsyncSession, promo_id: int) -> bool:
        result = await db.execute(
            select(ScheduledPromo).where(ScheduledPromo.id == promo_id)
        )
        promo = result.scalar_one_or_none()
        if not promo:
            return False
        await db.delete(promo)
        await db.flush()
        logger.info('Deleted scheduled promo', promo_id=promo_id)
        return True

    @staticmethod
    async def get_active_discount_for_tariff(db: AsyncSession, tariff_id: int) -> int | None:
        """Returns the highest active discount percent applicable to the given tariff, or None."""
        now = datetime.now(UTC)
        result = await db.execute(
            select(ScheduledPromo).where(
                and_(
                    ScheduledPromo.is_active.is_(True),
                    ScheduledPromo.start_at <= now,
                    ScheduledPromo.end_at >= now,
                )
            )
        )
        promos = result.scalars().all()
        best_discount: int | None = None
        for promo in promos:
            tariff_list = promo.tariff_ids or []
            if not tariff_list or tariff_id in tariff_list:
                if best_discount is None or promo.discount_percent > best_discount:
                    best_discount = promo.discount_percent
        return best_discount
