"""CRUD операции для платежей SeverPay."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import SeverPayPayment


logger = structlog.get_logger(__name__)


async def create_severpay_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'RUB',
    description: str | None = None,
    payment_url: str | None = None,
    payment_method: str | None = None,
    severpay_id: str | None = None,
    severpay_uid: str | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> SeverPayPayment:
    """Создает запись о платеже SeverPay."""
    payment = SeverPayPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        payment_url=payment_url,
        payment_method=payment_method,
        severpay_id=severpay_id,
        severpay_uid=severpay_uid,
        expires_at=expires_at,
        metadata_json=metadata_json,
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('Создан платеж SeverPay', order_id=order_id, user_id=user_id)
    return payment


async def get_severpay_payment_by_order_id(db: AsyncSession, order_id: str) -> SeverPayPayment | None:
    """Получает платеж по order_id (internal)."""
    result = await db.execute(select(SeverPayPayment).where(SeverPayPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_severpay_payment_by_severpay_id(db: AsyncSession, severpay_id: str) -> SeverPayPayment | None:
    """Получает платеж по ID от SeverPay."""
    result = await db.execute(select(SeverPayPayment).where(SeverPayPayment.severpay_id == severpay_id))
    return result.scalar_one_or_none()


async def get_severpay_payment_by_id(db: AsyncSession, payment_id: int) -> SeverPayPayment | None:
    """Получает платеж по ID."""
    result = await db.execute(select(SeverPayPayment).where(SeverPayPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_severpay_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> SeverPayPayment | None:
    """Получает платеж по ID с блокировкой FOR UPDATE."""
    result = await db.execute(select(SeverPayPayment).where(SeverPayPayment.id == payment_id).with_for_update())
    return result.scalar_one_or_none()


async def update_severpay_payment_status(
    db: AsyncSession,
    payment: SeverPayPayment,
    *,
    status: str,
    is_paid: bool | None = None,
    severpay_id: str | None = None,
    severpay_uid: str | None = None,
    payment_method: str | None = None,
    callback_payload: dict | None = None,
    transaction_id: int | None = None,
) -> SeverPayPayment:
    """Обновляет статус платежа."""
    payment.status = status
    payment.updated_at = datetime.now(UTC)

    if is_paid is not None:
        payment.is_paid = is_paid
        if is_paid:
            payment.paid_at = datetime.now(UTC)
    if severpay_id is not None:
        payment.severpay_id = severpay_id
    if severpay_uid is not None:
        payment.severpay_uid = severpay_uid
    if payment_method is not None:
        payment.payment_method = payment_method
    if callback_payload is not None:
        payment.callback_payload = callback_payload
    if transaction_id is not None:
        payment.transaction_id = transaction_id

    await db.commit()
    await db.refresh(payment)
    logger.info(
        'Обновлен статус платежа SeverPay',
        order_id=payment.order_id,
        status=status,
        is_paid=payment.is_paid,
    )
    return payment


async def get_pending_severpay_payments(db: AsyncSession, user_id: int) -> list[SeverPayPayment]:
    """Получает незавершенные платежи пользователя."""
    result = await db.execute(
        select(SeverPayPayment).where(
            SeverPayPayment.user_id == user_id,
            SeverPayPayment.status == 'pending',
            SeverPayPayment.is_paid == False,
        )
    )
    return list(result.scalars().all())


async def get_expired_pending_severpay_payments(
    db: AsyncSession,
) -> list[SeverPayPayment]:
    """Получает просроченные платежи в статусе pending."""
    now = datetime.now(UTC)
    result = await db.execute(
        select(SeverPayPayment).where(
            SeverPayPayment.status == 'pending',
            SeverPayPayment.is_paid == False,
            SeverPayPayment.expires_at < now,
        )
    )
    return list(result.scalars().all())


async def link_severpay_payment_to_transaction(
    db: AsyncSession,
    *,
    payment: SeverPayPayment,
    transaction_id: int,
) -> SeverPayPayment:
    """Связывает платеж с транзакцией."""
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)
    return payment
