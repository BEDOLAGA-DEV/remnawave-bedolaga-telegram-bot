"""CRUD операции для платежей RioPay."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import RioPayPayment


logger = structlog.get_logger(__name__)


async def create_riopay_payment(
    db: AsyncSession,
    *,
    user_id: int,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'RUB',
    description: str | None = None,
    payment_url: str | None = None,
    payment_method: str | None = None,
    riopay_order_id: str | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> RioPayPayment:
    """Создает запись о платеже RioPay."""
    payment = RioPayPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        payment_url=payment_url,
        payment_method=payment_method,
        riopay_order_id=riopay_order_id,
        expires_at=expires_at,
        metadata_json=metadata_json,
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('Создан платеж RioPay', order_id=order_id, user_id=user_id)
    return payment


async def get_riopay_payment_by_order_id(db: AsyncSession, order_id: str) -> RioPayPayment | None:
    """Получает платеж по order_id (internal)."""
    result = await db.execute(select(RioPayPayment).where(RioPayPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_riopay_payment_by_riopay_order_id(db: AsyncSession, riopay_order_id: str) -> RioPayPayment | None:
    """Получает платеж по ID от RioPay (UUID)."""
    result = await db.execute(select(RioPayPayment).where(RioPayPayment.riopay_order_id == riopay_order_id))
    return result.scalar_one_or_none()


async def get_riopay_payment_by_id(db: AsyncSession, payment_id: int) -> RioPayPayment | None:
    """Получает платеж по ID."""
    result = await db.execute(select(RioPayPayment).where(RioPayPayment.id == payment_id))
    return result.scalar_one_or_none()


async def update_riopay_payment_status(
    db: AsyncSession,
    payment: RioPayPayment,
    *,
    status: str,
    is_paid: bool | None = None,
    riopay_order_id: str | None = None,
    payment_method: str | None = None,
    callback_payload: dict | None = None,
    transaction_id: int | None = None,
) -> RioPayPayment:
    """Обновляет статус платежа."""
    payment.status = status
    payment.updated_at = datetime.now(UTC)

    if is_paid is not None:
        payment.is_paid = is_paid
        if is_paid:
            payment.paid_at = datetime.now(UTC)
    if riopay_order_id:
        payment.riopay_order_id = riopay_order_id
    if payment_method is not None:
        payment.payment_method = payment_method
    if callback_payload:
        payment.callback_payload = callback_payload
    if transaction_id:
        payment.transaction_id = transaction_id

    await db.commit()
    await db.refresh(payment)
    logger.info(
        'Обновлен статус платежа RioPay',
        order_id=payment.order_id,
        status=status,
        is_paid=payment.is_paid,
    )
    return payment


async def get_pending_riopay_payments(db: AsyncSession, user_id: int) -> list[RioPayPayment]:
    """Получает незавершенные платежи пользователя."""
    result = await db.execute(
        select(RioPayPayment).where(
            RioPayPayment.user_id == user_id,
            RioPayPayment.status == 'pending',
            RioPayPayment.is_paid == False,
        )
    )
    return list(result.scalars().all())


async def get_expired_pending_riopay_payments(
    db: AsyncSession,
) -> list[RioPayPayment]:
    """Получает просроченные платежи в статусе pending."""
    now = datetime.now(UTC)
    result = await db.execute(
        select(RioPayPayment).where(
            RioPayPayment.status == 'pending',
            RioPayPayment.is_paid == False,
            RioPayPayment.expires_at < now,
        )
    )
    return list(result.scalars().all())
