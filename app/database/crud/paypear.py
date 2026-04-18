"""CRUD операции для платежей PayPear."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PayPearPayment


logger = structlog.get_logger(__name__)


async def create_paypear_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'RUB',
    description: str | None = None,
    payment_url: str | None = None,
    payment_method: str | None = None,
    paypear_id: str | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> PayPearPayment:
    """Создает запись о платеже PayPear."""
    payment = PayPearPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        payment_url=payment_url,
        payment_method=payment_method,
        paypear_id=paypear_id,
        expires_at=expires_at,
        metadata_json=metadata_json,
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('Создан платеж PayPear', order_id=order_id, user_id=user_id)
    return payment


async def get_paypear_payment_by_order_id(db: AsyncSession, order_id: str) -> PayPearPayment | None:
    """Получает платеж по order_id (internal)."""
    result = await db.execute(select(PayPearPayment).where(PayPearPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_paypear_payment_by_paypear_id(db: AsyncSession, paypear_id: str) -> PayPearPayment | None:
    """Получает платеж по ID от PayPear."""
    result = await db.execute(select(PayPearPayment).where(PayPearPayment.paypear_id == paypear_id))
    return result.scalar_one_or_none()


async def get_paypear_payment_by_id(db: AsyncSession, payment_id: int) -> PayPearPayment | None:
    """Получает платеж по ID."""
    result = await db.execute(select(PayPearPayment).where(PayPearPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_paypear_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> PayPearPayment | None:
    """Получает платеж по ID с блокировкой FOR UPDATE."""
    result = await db.execute(
        select(PayPearPayment)
        .where(PayPearPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def update_paypear_payment_status(
    db: AsyncSession,
    payment: PayPearPayment,
    *,
    status: str,
    is_paid: bool | None = None,
    paypear_id: str | None = None,
    payment_method: str | None = None,
    callback_payload: dict | None = None,
    transaction_id: int | None = None,
) -> PayPearPayment:
    """Обновляет статус платежа."""
    payment.status = status
    payment.updated_at = datetime.now(UTC)

    if is_paid is not None:
        payment.is_paid = is_paid
        if is_paid:
            payment.paid_at = datetime.now(UTC)
    if paypear_id is not None:
        payment.paypear_id = paypear_id
    if payment_method is not None:
        payment.payment_method = payment_method
    if callback_payload is not None:
        payment.callback_payload = callback_payload
    if transaction_id is not None:
        payment.transaction_id = transaction_id

    await db.commit()
    await db.refresh(payment)
    logger.info(
        'Обновлен статус платежа PayPear',
        order_id=payment.order_id,
        status=status,
        is_paid=payment.is_paid,
    )
    return payment


async def get_pending_paypear_payments(db: AsyncSession, user_id: int) -> list[PayPearPayment]:
    """Получает незавершенные платежи пользователя."""
    result = await db.execute(
        select(PayPearPayment).where(
            PayPearPayment.user_id == user_id,
            PayPearPayment.status == 'pending',
            PayPearPayment.is_paid == False,
        )
    )
    return list(result.scalars().all())


async def get_expired_pending_paypear_payments(
    db: AsyncSession,
) -> list[PayPearPayment]:
    """Получает просроченные платежи в статусе pending."""
    now = datetime.now(UTC)
    result = await db.execute(
        select(PayPearPayment).where(
            PayPearPayment.status == 'pending',
            PayPearPayment.is_paid == False,
            PayPearPayment.expires_at < now,
        )
    )
    return list(result.scalars().all())


async def link_paypear_payment_to_transaction(
    db: AsyncSession,
    *,
    payment: PayPearPayment,
    transaction_id: int,
) -> PayPearPayment:
    """Связывает платеж с транзакцией."""
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)
    return payment
