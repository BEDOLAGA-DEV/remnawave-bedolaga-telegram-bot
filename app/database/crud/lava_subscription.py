"""CRUD для рекуррентных подписок Lava (зеркало platega_subscription)."""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import LavaSubscription


logger = structlog.get_logger(__name__)

_ACTIVE_STATUSES = ('PENDING', 'ACTIVE', 'PAST_DUE')


async def create_lava_subscription(
    db: AsyncSession,
    *,
    user_id: int,
    subscription_id: int,
    tariff_id: int | None,
    lava_product_id: str,
    lava_consumer_id: str | None,
    order_id: str,
    charge_days: int,
    amount_kopeks: int,
    redirect_url: str | None,
    lava_subscription_id: str | None,
    status: str = 'PENDING',
) -> LavaSubscription:
    record = LavaSubscription(
        user_id=user_id,
        subscription_id=subscription_id,
        tariff_id=tariff_id,
        lava_product_id=lava_product_id,
        lava_consumer_id=lava_consumer_id,
        order_id=order_id,
        charge_days=charge_days,
        amount_kopeks=amount_kopeks,
        redirect_url=redirect_url,
        lava_subscription_id=lava_subscription_id,
        status=status,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    logger.info('Создана Lava-подписка', lava_subscription_id=lava_subscription_id, user_id=user_id)
    return record


async def get_lava_subscription_by_id(db: AsyncSession, sub_id: int) -> LavaSubscription | None:
    return await db.get(LavaSubscription, sub_id)


async def get_lava_subscription_by_id_for_update(db: AsyncSession, sub_id: int) -> LavaSubscription | None:
    result = await db.execute(
        select(LavaSubscription)
        .where(LavaSubscription.id == sub_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_lava_subscription_by_lava_id(db: AsyncSession, lava_id: str) -> LavaSubscription | None:
    result = await db.execute(select(LavaSubscription).where(LavaSubscription.lava_subscription_id == lava_id))
    return result.scalar_one_or_none()


async def get_lava_subscription_by_order_id(db: AsyncSession, order_id: str) -> LavaSubscription | None:
    """Поиск по orderId — основной путь вебхука: списание приходит инвойсом."""
    result = await db.execute(select(LavaSubscription).where(LavaSubscription.order_id == order_id))
    return result.scalar_one_or_none()


async def get_active_lava_subscription_by_subscription(
    db: AsyncSession, subscription_id: int
) -> LavaSubscription | None:
    result = await db.execute(
        select(LavaSubscription)
        .where(
            LavaSubscription.subscription_id == subscription_id,
            LavaSubscription.status.in_(_ACTIVE_STATUSES),
        )
        .order_by(LavaSubscription.id.desc())
    )
    return result.scalars().first()


async def update_lava_subscription(db: AsyncSession, record: LavaSubscription, **fields: Any) -> LavaSubscription:
    for key, value in fields.items():
        setattr(record, key, value)
    await db.commit()
    await db.refresh(record)
    return record


async def list_lava_subscriptions_by_statuses(db: AsyncSession, statuses: list[str]) -> list[LavaSubscription]:
    result = await db.execute(select(LavaSubscription).where(LavaSubscription.status.in_(statuses)))
    return list(result.scalars().all())


async def list_recently_cancelled_lava_subscriptions(db: AsyncSession, updated_after: Any) -> list[LavaSubscription]:
    """Недавно отменённые локально записи с remote-идентификатором.

    Нужны reconciler'у: локальная отмена могла не дойти до Lava (сеть), и
    провайдер продолжил бы списывать.
    """
    result = await db.execute(
        select(LavaSubscription).where(
            LavaSubscription.status == 'CANCELLED',
            LavaSubscription.lava_subscription_id.isnot(None),
            LavaSubscription.updated_at >= updated_after,
        )
    )
    return list(result.scalars().all())
