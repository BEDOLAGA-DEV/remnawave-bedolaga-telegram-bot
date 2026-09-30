"""CRUD для подписок Cashera (автопродление; зеркало lava_subscription)."""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CasheraSubscription


logger = structlog.get_logger(__name__)

_ACTIVE_STATUSES = ('PENDING', 'ACTIVE', 'PAST_DUE')


async def create_cashera_subscription(
    db: AsyncSession,
    *,
    user_id: int,
    subscription_id: int,
    tariff_id: int | None,
    external_id: str,
    interval: str,
    charge_days: int,
    amount_kopeks: int,
    redirect_url: str | None,
    cashera_subscription_uuid: str | None,
    remote_status: str | None = None,
    status: str = 'PENDING',
) -> CasheraSubscription:
    record = CasheraSubscription(
        user_id=user_id,
        subscription_id=subscription_id,
        tariff_id=tariff_id,
        external_id=external_id,
        interval=interval,
        charge_days=charge_days,
        amount_kopeks=amount_kopeks,
        redirect_url=redirect_url,
        cashera_subscription_uuid=cashera_subscription_uuid,
        remote_status=remote_status,
        status=status,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    logger.info('Создана подписка Cashera', cashera_uuid=cashera_subscription_uuid, user_id=user_id)
    return record


async def get_cashera_subscription_by_id(db: AsyncSession, sub_id: int) -> CasheraSubscription | None:
    return await db.get(CasheraSubscription, sub_id)


async def get_cashera_subscription_by_id_for_update(db: AsyncSession, sub_id: int) -> CasheraSubscription | None:
    result = await db.execute(
        select(CasheraSubscription)
        .where(CasheraSubscription.id == sub_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_cashera_subscription_by_uuid(db: AsyncSession, cashera_uuid: str) -> CasheraSubscription | None:
    result = await db.execute(
        select(CasheraSubscription).where(CasheraSubscription.cashera_subscription_uuid == cashera_uuid)
    )
    return result.scalar_one_or_none()


async def get_cashera_subscription_by_external_id(db: AsyncSession, external_id: str) -> CasheraSubscription | None:
    result = await db.execute(select(CasheraSubscription).where(CasheraSubscription.external_id == external_id))
    return result.scalar_one_or_none()


async def get_active_cashera_subscription_by_subscription(
    db: AsyncSession, subscription_id: int
) -> CasheraSubscription | None:
    result = await db.execute(
        select(CasheraSubscription)
        .where(
            CasheraSubscription.subscription_id == subscription_id,
            CasheraSubscription.status.in_(_ACTIVE_STATUSES),
        )
        .order_by(CasheraSubscription.id.desc())
    )
    return result.scalars().first()


async def update_cashera_subscription(
    db: AsyncSession, record: CasheraSubscription, **fields: Any
) -> CasheraSubscription:
    for key, value in fields.items():
        setattr(record, key, value)
    await db.commit()
    await db.refresh(record)
    return record


async def list_cashera_subscriptions_by_statuses(db: AsyncSession, statuses: list[str]) -> list[CasheraSubscription]:
    result = await db.execute(select(CasheraSubscription).where(CasheraSubscription.status.in_(statuses)))
    return list(result.scalars().all())


async def list_recently_cancelled_cashera_subscriptions(
    db: AsyncSession, updated_after: Any
) -> list[CasheraSubscription]:
    """Недавно отменённые локально записи с remote-идентификатором.

    Нужны reconciler'у: локальная отмена могла не дойти до Cashera (сеть), и
    провайдер продолжил бы списывать.
    """
    result = await db.execute(
        select(CasheraSubscription).where(
            CasheraSubscription.status == 'CANCELLED',
            CasheraSubscription.cashera_subscription_uuid.isnot(None),
            CasheraSubscription.updated_at >= updated_after,
        )
    )
    return list(result.scalars().all())
