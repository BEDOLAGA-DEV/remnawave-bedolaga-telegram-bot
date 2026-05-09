"""CRUD операции для платежей LOLZ."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import LolzPayment


logger = structlog.get_logger(__name__)


async def create_lolz_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'RUB',
    description: str | None = None,
    payment_url: str | None = None,
    payment_method: str | None = None,
    lolz_invoice_id: int | None = None,
    lolz_payment_id: str | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> LolzPayment:
    """Создает запись о платеже LOLZ."""
    payment = LolzPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        payment_url=payment_url,
        payment_method=payment_method,
        lolz_invoice_id=lolz_invoice_id,
        lolz_payment_id=lolz_payment_id,
        expires_at=expires_at,
        metadata_json=metadata_json,
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('Создан платеж LOLZ', order_id=order_id, user_id=user_id)
    return payment


async def get_lolz_payment_by_order_id(db: AsyncSession, order_id: str) -> LolzPayment | None:
    """Получает платеж по order_id (internal)."""
    result = await db.execute(select(LolzPayment).where(LolzPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_lolz_payment_by_invoice_id(db: AsyncSession, lolz_invoice_id: int) -> LolzPayment | None:
    """Получает платеж по invoice_id от LOLZ."""
    result = await db.execute(select(LolzPayment).where(LolzPayment.lolz_invoice_id == lolz_invoice_id))
    return result.scalar_one_or_none()


async def get_lolz_payment_by_payment_id(db: AsyncSession, lolz_payment_id: str) -> LolzPayment | None:
    """Получает платеж по нашему payment_id, отправленному в LOLZ."""
    result = await db.execute(select(LolzPayment).where(LolzPayment.lolz_payment_id == lolz_payment_id))
    return result.scalar_one_or_none()


async def get_lolz_payment_by_id(db: AsyncSession, payment_id: int) -> LolzPayment | None:
    """Получает платеж по ID."""
    result = await db.execute(select(LolzPayment).where(LolzPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_lolz_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> LolzPayment | None:
    """Получает платеж по ID с блокировкой FOR UPDATE."""
    result = await db.execute(
        select(LolzPayment)
        .where(LolzPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def update_lolz_payment_status(
    db: AsyncSession,
    payment: LolzPayment,
    *,
    status: str,
    is_paid: bool | None = None,
    lolz_invoice_id: int | None = None,
    lolz_payment_id: str | None = None,
    payment_method: str | None = None,
    callback_payload: dict | None = None,
    transaction_id: int | None = None,
) -> LolzPayment:
    """Обновляет статус платежа."""
    payment.status = status
    payment.updated_at = datetime.now(UTC)

    if is_paid is not None:
        payment.is_paid = is_paid
        if is_paid:
            payment.paid_at = datetime.now(UTC)
    if lolz_invoice_id is not None:
        payment.lolz_invoice_id = lolz_invoice_id
    if lolz_payment_id is not None:
        payment.lolz_payment_id = lolz_payment_id
    if payment_method is not None:
        payment.payment_method = payment_method
    if callback_payload is not None:
        payment.callback_payload = callback_payload
    if transaction_id is not None:
        payment.transaction_id = transaction_id

    await db.commit()
    await db.refresh(payment)
    logger.info(
        'Обновлен статус платежа LOLZ',
        order_id=payment.order_id,
        status=status,
        is_paid=payment.is_paid,
    )
    return payment


async def get_pending_lolz_payments(db: AsyncSession, user_id: int) -> list[LolzPayment]:
    """Получает незавершенные платежи пользователя."""
    result = await db.execute(
        select(LolzPayment).where(
            LolzPayment.user_id == user_id,
            LolzPayment.status == 'pending',
            LolzPayment.is_paid == False,
        )
    )
    return list(result.scalars().all())


async def get_expired_pending_lolz_payments(
    db: AsyncSession,
) -> list[LolzPayment]:
    """Получает просроченные платежи в статусе pending."""
    now = datetime.now(UTC)
    result = await db.execute(
        select(LolzPayment).where(
            LolzPayment.status == 'pending',
            LolzPayment.is_paid == False,
            LolzPayment.expires_at < now,
        )
    )
    return list(result.scalars().all())


async def link_lolz_payment_to_transaction(
    db: AsyncSession,
    *,
    payment: LolzPayment,
    transaction_id: int,
) -> LolzPayment:
    """Связывает платеж с транзакцией."""
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)
    return payment
