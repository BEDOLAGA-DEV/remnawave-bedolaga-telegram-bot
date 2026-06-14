"""CRUD для рекуррентных подписок Antilopay."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AntilopayRecurrent


logger = structlog.get_logger(__name__)


def _build_recurrent_title(pay_method: str | None, pay_data: str | None) -> str | None:
    if pay_data:
        method_label = pay_method or 'CARD_RU'
        if method_label == 'CARD_RU':
            method_label = 'Card'
        return f'{method_label} {pay_data}'
    return None


async def upsert_antilopay_recurrent(
    db: AsyncSession,
    *,
    user_id: int,
    recurrent_id: str,
    initial_payment_id: str | None = None,
    recurrent_type: str | None = None,
    payment_count: int | None = None,
    status: str | None = None,
    pay_method: str | None = None,
    pay_data: str | None = None,
    subscription_id: int | None = None,
) -> AntilopayRecurrent:
    """Создаёт или обновляет запись рекуррента Antilopay."""
    result = await db.execute(
        select(AntilopayRecurrent).where(AntilopayRecurrent.recurrent_id == recurrent_id)
    )
    existing = result.scalar_one_or_none()
    title = _build_recurrent_title(pay_method, pay_data)

    if existing:
        existing.user_id = user_id
        if subscription_id is not None:
            existing.subscription_id = subscription_id
        if initial_payment_id:
            existing.initial_payment_id = initial_payment_id
        if recurrent_type:
            existing.recurrent_type = recurrent_type
        if payment_count is not None:
            existing.payment_count = payment_count
        if status:
            existing.status = status
        if pay_method:
            existing.pay_method = pay_method
        if pay_data:
            existing.pay_data = pay_data
        if title:
            existing.title = title
        if status in {'CANCEL', 'PROVIDER_CANCEL', 'COMPLETE', 'ERROR'}:
            existing.is_active = False
        existing.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(existing)
        return existing

    recurrent = AntilopayRecurrent(
        user_id=user_id,
        subscription_id=subscription_id,
        recurrent_id=recurrent_id,
        initial_payment_id=initial_payment_id,
        recurrent_type=recurrent_type or 'MONTH',
        payment_count=payment_count,
        status=status,
        pay_method=pay_method,
        pay_data=pay_data,
        title=title,
        is_active=True,
    )
    db.add(recurrent)
    await db.commit()
    await db.refresh(recurrent)
    logger.info(
        'Создан рекуррент Antilopay',
        user_id=user_id,
        recurrent_id=recurrent_id,
    )
    return recurrent


async def get_active_antilopay_recurrents_by_user(
    db: AsyncSession,
    user_id: int,
) -> list[AntilopayRecurrent]:
    result = await db.execute(
        select(AntilopayRecurrent)
        .where(
            AntilopayRecurrent.user_id == user_id,
            AntilopayRecurrent.is_active == True,
        )
        .order_by(AntilopayRecurrent.created_at.desc())
    )
    return list(result.scalars().all())


async def get_antilopay_recurrent_by_id(
    db: AsyncSession,
    recurrent_id: int,
    *,
    user_id: int | None = None,
) -> AntilopayRecurrent | None:
    query = select(AntilopayRecurrent).where(AntilopayRecurrent.id == recurrent_id)
    if user_id is not None:
        query = query.where(AntilopayRecurrent.user_id == user_id)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def deactivate_antilopay_recurrent(
    db: AsyncSession,
    recurrent: AntilopayRecurrent,
    *,
    status: str | None = 'CANCEL',
) -> AntilopayRecurrent:
    recurrent.is_active = False
    if status:
        recurrent.status = status
    recurrent.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(recurrent)
    return recurrent


async def deactivate_all_antilopay_recurrents_for_user(
    db: AsyncSession,
    user_id: int,
) -> int:
    result = await db.execute(
        update(AntilopayRecurrent)
        .where(
            AntilopayRecurrent.user_id == user_id,
            AntilopayRecurrent.is_active == True,
        )
        .values(is_active=False, status='CANCEL', updated_at=datetime.now(UTC))
    )
    await db.commit()
    return result.rowcount or 0
