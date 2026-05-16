"""CRUD операции для платежей Jupiter (FPGate P2P v2.1)."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import JupiterPayment


logger = structlog.get_logger(__name__)


async def create_jupiter_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'RUB',
    description: str | None = None,
    payment_url: str | None = None,
    payment_method: str | None = None,
    jupiter_transaction_id: str | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> JupiterPayment:
    """Создаёт запись о платеже Jupiter."""
    payment = JupiterPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        payment_url=payment_url,
        payment_method=payment_method,
        jupiter_transaction_id=jupiter_transaction_id,
        expires_at=expires_at,
        metadata_json=metadata_json,
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('Создан платеж Jupiter', order_id=order_id, user_id=user_id)
    return payment


async def get_jupiter_payment_by_order_id(db: AsyncSession, order_id: str) -> JupiterPayment | None:
    """Получает платеж по order_id (internal)."""
    result = await db.execute(select(JupiterPayment).where(JupiterPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_jupiter_payment_by_invoice_id(db: AsyncSession, jupiter_transaction_id: str) -> JupiterPayment | None:
    """Получает платёж по transaction_id, выданному Jupiter."""
    result = await db.execute(
        select(JupiterPayment).where(JupiterPayment.jupiter_transaction_id == jupiter_transaction_id)
    )
    return result.scalar_one_or_none()


async def get_jupiter_payment_by_id(db: AsyncSession, payment_id: int) -> JupiterPayment | None:
    """Получает платеж по локальному ID."""
    result = await db.execute(select(JupiterPayment).where(JupiterPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_jupiter_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> JupiterPayment | None:
    """Получает платёж с блокировкой FOR UPDATE."""
    result = await db.execute(
        select(JupiterPayment)
        .where(JupiterPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def update_jupiter_payment_status(
    db: AsyncSession,
    payment: JupiterPayment,
    *,
    status: str,
    is_paid: bool | None = None,
    jupiter_transaction_id: str | None = None,
    payment_method: str | None = None,
    callback_payload: dict | None = None,
    transaction_id: int | None = None,
) -> JupiterPayment:
    """Обновляет статус платежа."""
    payment.status = status
    payment.updated_at = datetime.now(UTC)

    if is_paid is not None:
        payment.is_paid = is_paid
        if is_paid:
            payment.paid_at = datetime.now(UTC)
    if jupiter_transaction_id is not None:
        payment.jupiter_transaction_id = jupiter_transaction_id
    if payment_method is not None:
        payment.payment_method = payment_method
    if callback_payload is not None:
        payment.callback_payload = callback_payload
    if transaction_id is not None:
        payment.transaction_id = transaction_id

    await db.commit()
    await db.refresh(payment)
    logger.info(
        'Обновлён статус платежа Jupiter',
        order_id=payment.order_id,
        status=status,
        is_paid=payment.is_paid,
    )
    return payment


async def get_pending_jupiter_payments(db: AsyncSession, user_id: int) -> list[JupiterPayment]:
    """Возвращает незавершённые платежи пользователя."""
    result = await db.execute(
        select(JupiterPayment).where(
            JupiterPayment.user_id == user_id,
            JupiterPayment.status == 'pending',
            JupiterPayment.is_paid == False,
        )
    )
    return list(result.scalars().all())


async def get_expired_pending_jupiter_payments(db: AsyncSession) -> list[JupiterPayment]:
    """Возвращает просроченные платежи в статусе pending."""
    now = datetime.now(UTC)
    result = await db.execute(
        select(JupiterPayment).where(
            JupiterPayment.status == 'pending',
            JupiterPayment.is_paid == False,
            JupiterPayment.expires_at < now,
        )
    )
    return list(result.scalars().all())


async def link_jupiter_payment_to_transaction(
    db: AsyncSession,
    *,
    payment: JupiterPayment,
    transaction_id: int,
) -> JupiterPayment:
    """Связывает платёж с транзакцией."""
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)
    return payment
