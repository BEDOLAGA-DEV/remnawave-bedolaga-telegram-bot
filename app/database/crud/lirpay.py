"""CRUD операции для платежей LirPay (lirpay.org, Integration API v2)."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import LirPayPayment


logger = structlog.get_logger(__name__)


async def create_lirpay_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'RUB',
    description: str | None = None,
    payment_url: str | None = None,
    payment_method: str | None = None,
    lirpay_payment_id: str | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> LirPayPayment:
    """Создаёт запись о платеже LirPay."""
    payment = LirPayPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        payment_url=payment_url,
        payment_method=payment_method,
        lirpay_payment_id=lirpay_payment_id,
        expires_at=expires_at,
        metadata_json=metadata_json,
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('Создан платеж LirPay', order_id=order_id, user_id=user_id)
    return payment


async def get_lirpay_payment_by_order_id(db: AsyncSession, order_id: str) -> LirPayPayment | None:
    """Получает платёж по нашему order_id (= Idempotency-Key)."""
    result = await db.execute(select(LirPayPayment).where(LirPayPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_lirpay_payment_by_invoice_id(db: AsyncSession, lirpay_payment_id: str) -> LirPayPayment | None:
    """Получает платёж по public_id, выданному LirPay."""
    result = await db.execute(select(LirPayPayment).where(LirPayPayment.lirpay_payment_id == lirpay_payment_id))
    return result.scalar_one_or_none()


async def get_lirpay_payment_by_id(db: AsyncSession, payment_id: int) -> LirPayPayment | None:
    """Получает платёж по локальному ID."""
    result = await db.execute(select(LirPayPayment).where(LirPayPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_lirpay_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> LirPayPayment | None:
    """Получает платёж с блокировкой FOR UPDATE.

    Вебхук LirPay и фоновая сверка могут прийти одновременно: без блокировки
    баланс был бы начислен дважды.
    """
    result = await db.execute(
        select(LirPayPayment)
        .where(LirPayPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def update_lirpay_payment_status(
    db: AsyncSession,
    payment: LirPayPayment,
    *,
    status: str,
    is_paid: bool | None = None,
    lirpay_payment_id: str | None = None,
    payment_method: str | None = None,
    callback_payload: dict | None = None,
    transaction_id: int | None = None,
) -> LirPayPayment:
    """Обновляет статус платежа."""
    payment.status = status
    payment.updated_at = datetime.now(UTC)

    if is_paid is not None:
        payment.is_paid = is_paid
        if is_paid:
            payment.paid_at = datetime.now(UTC)
    if lirpay_payment_id is not None:
        payment.lirpay_payment_id = lirpay_payment_id
    if payment_method is not None:
        payment.payment_method = payment_method
    if callback_payload is not None:
        payment.callback_payload = callback_payload
    if transaction_id is not None:
        payment.transaction_id = transaction_id

    await db.commit()
    await db.refresh(payment)
    logger.info(
        'Обновлён статус платежа LirPay',
        order_id=payment.order_id,
        status=status,
        is_paid=payment.is_paid,
    )
    return payment


async def get_pending_lirpay_payments(db: AsyncSession, user_id: int) -> list[LirPayPayment]:
    """Возвращает незавершённые платежи пользователя."""
    result = await db.execute(
        select(LirPayPayment).where(
            LirPayPayment.user_id == user_id,
            LirPayPayment.status == 'pending',
            LirPayPayment.is_paid == False,
        )
    )
    return list(result.scalars().all())


async def link_lirpay_payment_to_transaction(
    db: AsyncSession,
    *,
    payment: LirPayPayment,
    transaction_id: int,
) -> LirPayPayment:
    """Связывает платёж с транзакцией."""
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)
    return payment
