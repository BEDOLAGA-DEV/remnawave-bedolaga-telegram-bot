"""CRUD операции для платежей Lava (Lava Business)."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import LavaPayment


logger = structlog.get_logger(__name__)


async def create_lava_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'RUB',
    description: str | None = None,
    payment_url: str | None = None,
    payment_method: str | None = None,
    lava_invoice_id: str | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> LavaPayment:
    """Создаёт запись о платеже Lava."""
    payment = LavaPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        payment_url=payment_url,
        payment_method=payment_method,
        lava_invoice_id=lava_invoice_id,
        expires_at=expires_at,
        metadata_json=metadata_json,
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('Создан платеж Lava', order_id=order_id, user_id=user_id)
    return payment


async def get_lava_payment_by_order_id(db: AsyncSession, order_id: str) -> LavaPayment | None:
    """Получает платёж по нашему orderId."""
    result = await db.execute(select(LavaPayment).where(LavaPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_lava_payment_by_invoice_id(db: AsyncSession, lava_invoice_id: str) -> LavaPayment | None:
    """Получает платёж по invoice_id, выданному Lava."""
    result = await db.execute(select(LavaPayment).where(LavaPayment.lava_invoice_id == lava_invoice_id))
    return result.scalar_one_or_none()


async def get_lava_payment_by_id(db: AsyncSession, payment_id: int) -> LavaPayment | None:
    """Получает платёж по локальному ID."""
    result = await db.execute(select(LavaPayment).where(LavaPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_lava_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> LavaPayment | None:
    """Получает платёж с FOR UPDATE-блокировкой."""
    result = await db.execute(
        select(LavaPayment)
        .where(LavaPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def update_lava_payment_status(
    db: AsyncSession,
    payment: LavaPayment,
    *,
    status: str,
    is_paid: bool | None = None,
    lava_invoice_id: str | None = None,
    payment_method: str | None = None,
    callback_payload: dict | None = None,
    transaction_id: int | None = None,
) -> LavaPayment:
    """Обновляет статус платежа."""
    payment.status = status
    payment.updated_at = datetime.now(UTC)

    if is_paid is not None:
        payment.is_paid = is_paid
        if is_paid:
            payment.paid_at = datetime.now(UTC)
    if lava_invoice_id is not None:
        payment.lava_invoice_id = lava_invoice_id
    if payment_method is not None:
        payment.payment_method = payment_method
    if callback_payload is not None:
        payment.callback_payload = callback_payload
    if transaction_id is not None:
        payment.transaction_id = transaction_id

    await db.commit()
    await db.refresh(payment)
    logger.info(
        'Обновлён статус платежа Lava',
        order_id=payment.order_id,
        status=status,
        is_paid=payment.is_paid,
    )
    return payment


async def get_pending_lava_payments(db: AsyncSession, user_id: int) -> list[LavaPayment]:
    """Возвращает незавершённые платежи пользователя."""
    result = await db.execute(
        select(LavaPayment).where(
            LavaPayment.user_id == user_id,
            LavaPayment.status == 'pending',
            LavaPayment.is_paid == False,
        )
    )
    return list(result.scalars().all())


async def get_expired_pending_lava_payments(db: AsyncSession) -> list[LavaPayment]:
    """Возвращает просроченные платежи в статусе pending."""
    now = datetime.now(UTC)
    result = await db.execute(
        select(LavaPayment).where(
            LavaPayment.status == 'pending',
            LavaPayment.is_paid == False,
            LavaPayment.expires_at < now,
        )
    )
    return list(result.scalars().all())


async def link_lava_payment_to_transaction(
    db: AsyncSession,
    *,
    payment: LavaPayment,
    transaction_id: int,
) -> LavaPayment:
    """Связывает платёж с транзакцией."""
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)
    return payment
