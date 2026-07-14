"""CRUD for UnitPay payments."""
from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import UnitPayPayment

logger = structlog.get_logger(__name__)


async def create_unitpay_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'RUB',
    description: str | None = None,
    payment_url: str | None = None,
    payment_type: str | None = None,
    unitpay_id: str | None = None,
    subscription_id: str | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> UnitPayPayment:
    payment = UnitPayPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        payment_url=payment_url,
        payment_type=payment_type,
        unitpay_id=unitpay_id,
        subscription_id=subscription_id,
        expires_at=expires_at,
        metadata_json=metadata_json,
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('Создан платеж UnitPay', order_id=order_id, user_id=user_id)
    return payment


async def get_unitpay_payment_by_order_id(db: AsyncSession, order_id: str) -> UnitPayPayment | None:
    result = await db.execute(select(UnitPayPayment).where(UnitPayPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_unitpay_payment_by_unitpay_id(db: AsyncSession, unitpay_id: str) -> UnitPayPayment | None:
    result = await db.execute(select(UnitPayPayment).where(UnitPayPayment.unitpay_id == unitpay_id))
    return result.scalar_one_or_none()


async def get_unitpay_payment_by_id(db: AsyncSession, payment_id: int) -> UnitPayPayment | None:
    result = await db.execute(select(UnitPayPayment).where(UnitPayPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_unitpay_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> UnitPayPayment | None:
    result = await db.execute(
        select(UnitPayPayment).where(UnitPayPayment.id == payment_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def update_unitpay_payment_status(
    db: AsyncSession,
    payment_id: int,
    *,
    status: str | None = None,
    is_paid: bool | None = None,
    unitpay_id: str | None = None,
    subscription_id: str | None = None,
    callback_payload: dict | None = None,
    paid_at: datetime | None = None,
    transaction_id: int | None = None,
) -> None:
    values: dict = {}
    if status is not None:
        values['status'] = status
    if is_paid is not None:
        values['is_paid'] = is_paid
    if unitpay_id is not None:
        values['unitpay_id'] = unitpay_id
    if subscription_id is not None:
        values['subscription_id'] = subscription_id
    if callback_payload is not None:
        values['callback_payload'] = callback_payload
    if paid_at is not None:
        values['paid_at'] = paid_at
    if transaction_id is not None:
        values['transaction_id'] = transaction_id
    if not values:
        return
    values['updated_at'] = datetime.now(UTC)
    await db.execute(update(UnitPayPayment).where(UnitPayPayment.id == payment_id).values(**values))
    await db.commit()


async def get_pending_unitpay_payments(db: AsyncSession, user_id: int) -> list[UnitPayPayment]:
    result = await db.execute(
        select(UnitPayPayment).where(
            UnitPayPayment.user_id == user_id,
            UnitPayPayment.status == 'pending',
            UnitPayPayment.is_paid.is_(False),
        )
    )
    return list(result.scalars().all())


async def get_expired_pending_unitpay_payments(db: AsyncSession) -> list[UnitPayPayment]:
    result = await db.execute(
        select(UnitPayPayment).where(
            UnitPayPayment.status == 'pending',
            UnitPayPayment.is_paid.is_(False),
            UnitPayPayment.expires_at.isnot(None),
            UnitPayPayment.expires_at < datetime.now(UTC),
        )
    )
    return list(result.scalars().all())
