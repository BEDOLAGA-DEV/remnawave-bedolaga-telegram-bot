"""CRUD операции для платежей RollyPay."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import RollyPayPayment


logger = structlog.get_logger(__name__)


async def create_rollypay_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'RUB',
    description: str | None = None,
    payment_url: str | None = None,
    payment_method: str | None = None,
    rollypay_payment_id: str | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> RollyPayPayment:
    """Создает запись о платеже RollyPay."""
    payment = RollyPayPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        payment_url=payment_url,
        payment_method=payment_method,
        rollypay_payment_id=rollypay_payment_id,
        expires_at=expires_at,
        metadata_json=metadata_json,
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('Создан платеж RollyPay', order_id=order_id, user_id=user_id)
    return payment


async def get_rollypay_payment_by_order_id(db: AsyncSession, order_id: str) -> RollyPayPayment | None:
    """Получает платеж по order_id (internal)."""
    result = await db.execute(select(RollyPayPayment).where(RollyPayPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_rollypay_payment_by_rollypay_id(db: AsyncSession, rollypay_payment_id: str) -> RollyPayPayment | None:
    """Получает платеж по ID от RollyPay."""
    result = await db.execute(select(RollyPayPayment).where(RollyPayPayment.rollypay_payment_id == rollypay_payment_id))
    return result.scalar_one_or_none()


async def get_rollypay_payment_by_id(db: AsyncSession, payment_id: int) -> RollyPayPayment | None:
    """Получает платеж по ID."""
    result = await db.execute(select(RollyPayPayment).where(RollyPayPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_rollypay_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> RollyPayPayment | None:
    """Получает платеж по ID с блокировкой FOR UPDATE."""
    result = await db.execute(
        select(RollyPayPayment)
        .where(RollyPayPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def update_rollypay_payment_status(
    db: AsyncSession,
    payment: RollyPayPayment,
    *,
    status: str,
    is_paid: bool | None = None,
    rollypay_payment_id: str | None = None,
    payment_method: str | None = None,
    callback_payload: dict | None = None,
    transaction_id: int | None = None,
) -> RollyPayPayment:
    """Обновляет статус платежа."""
    payment.status = status
    payment.updated_at = datetime.now(UTC)

    if is_paid is not None:
        payment.is_paid = is_paid
        if is_paid:
            payment.paid_at = datetime.now(UTC)
    if rollypay_payment_id is not None:
        payment.rollypay_payment_id = rollypay_payment_id
    if payment_method is not None:
        payment.payment_method = payment_method
    if callback_payload is not None:
        payment.callback_payload = callback_payload
    if transaction_id is not None:
        payment.transaction_id = transaction_id

    await db.commit()
    await db.refresh(payment)
    logger.info(
        'Обновлен статус платежа RollyPay',
        order_id=payment.order_id,
        status=status,
        is_paid=payment.is_paid,
    )
    return payment


async def get_pending_rollypay_payments(db: AsyncSession, user_id: int) -> list[RollyPayPayment]:
    """Получает незавершенные платежи пользователя."""
    result = await db.execute(
        select(RollyPayPayment).where(
            RollyPayPayment.user_id == user_id,
            RollyPayPayment.status == 'pending',
            RollyPayPayment.is_paid == False,
        )
    )
    return list(result.scalars().all())


async def get_expired_pending_rollypay_payments(
    db: AsyncSession,
) -> list[RollyPayPayment]:
    """Получает просроченные платежи в статусе pending."""
    now = datetime.now(UTC)
    result = await db.execute(
        select(RollyPayPayment).where(
            RollyPayPayment.status == 'pending',
            RollyPayPayment.is_paid == False,
            RollyPayPayment.expires_at < now,
        )
    )
    return list(result.scalars().all())


async def link_rollypay_payment_to_transaction(
    db: AsyncSession,
    *,
    payment: RollyPayPayment,
    transaction_id: int,
) -> RollyPayPayment:
    """Связывает платеж с транзакцией."""
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)
    return payment
