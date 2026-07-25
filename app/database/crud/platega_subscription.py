"""CRUD для рекуррентных СБП-подписок Platega."""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PlategaSubscription


logger = structlog.get_logger(__name__)

_ACTIVE_STATUSES = ('PENDING', 'ACTIVE', 'PAST_DUE')


async def create_platega_subscription(
    db: AsyncSession,
    *,
    user_id: int,
    subscription_id: int,
    tariff_id: int | None,
    interval: int,
    charge_days: int,
    amount_kopeks: int,
    redirect_url: str | None,
    platega_subscription_id: str | None,
    status: str = 'PENDING',
) -> PlategaSubscription:
    record = PlategaSubscription(
        user_id=user_id,
        subscription_id=subscription_id,
        tariff_id=tariff_id,
        interval=interval,
        charge_days=charge_days,
        amount_kopeks=amount_kopeks,
        redirect_url=redirect_url,
        platega_subscription_id=platega_subscription_id,
        status=status,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    logger.info('Создана Platega-подписка', platega_subscription_id=platega_subscription_id, user_id=user_id)
    return record


async def get_platega_subscription_by_id(db: AsyncSession, sub_id: int) -> PlategaSubscription | None:
    return await db.get(PlategaSubscription, sub_id)


async def get_platega_subscription_by_id_for_update(db: AsyncSession, sub_id: int) -> PlategaSubscription | None:
    result = await db.execute(
        select(PlategaSubscription)
        .where(PlategaSubscription.id == sub_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_platega_subscription_by_platega_id(db: AsyncSession, platega_id: str) -> PlategaSubscription | None:
    result = await db.execute(
        select(PlategaSubscription).where(PlategaSubscription.platega_subscription_id == platega_id)
    )
    return result.scalar_one_or_none()


async def get_active_platega_subscription_by_subscription(
    db: AsyncSession, subscription_id: int
) -> PlategaSubscription | None:
    result = await db.execute(
        select(PlategaSubscription)
        .where(
            PlategaSubscription.subscription_id == subscription_id,
            PlategaSubscription.status.in_(_ACTIVE_STATUSES),
        )
        .order_by(PlategaSubscription.id.desc())
    )
    return result.scalars().first()


async def update_platega_subscription(
    db: AsyncSession, record: PlategaSubscription, **fields: Any
) -> PlategaSubscription:
    for key, value in fields.items():
        setattr(record, key, value)
    await db.commit()
    await db.refresh(record)
    return record


async def list_platega_subscriptions_by_statuses(db: AsyncSession, statuses: list[str]) -> list[PlategaSubscription]:
    result = await db.execute(select(PlategaSubscription).where(PlategaSubscription.status.in_(statuses)))
    return list(result.scalars().all())


async def list_recently_cancelled_platega_subscriptions(
    db: AsyncSession, updated_after: Any
) -> list[PlategaSubscription]:
    """Недавно отменённые локально записи с remote-идентификатором.

    Нужны reconciler'у для контрольной сверки: локальная отмена могла не
    дойти до Platega (сеть/аутентификация), и провайдер продолжил бы
    списывать. Окно ``updated_after`` ограничивает свип свежими отменами.
    """
    result = await db.execute(
        select(PlategaSubscription).where(
            PlategaSubscription.status == 'CANCELLED',
            PlategaSubscription.platega_subscription_id.isnot(None),
            PlategaSubscription.updated_at >= updated_after,
        )
    )
    return list(result.scalars().all())
