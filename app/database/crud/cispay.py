"""CRUD операции для платежей cisPay (api.cispay.app)."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CisPayPayment


logger = structlog.get_logger(__name__)


async def create_cispay_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'RUB',
    description: str | None = None,
    payment_url: str | None = None,
    payment_method: str | None = None,
    cispay_payment_id: str | None = None,
    charged_amount_kopeks: int | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> CisPayPayment:
    """Создаёт запись о платеже cisPay."""
    payment = CisPayPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        payment_url=payment_url,
        payment_method=payment_method,
        cispay_payment_id=cispay_payment_id,
        charged_amount_kopeks=charged_amount_kopeks,
        expires_at=expires_at,
        metadata_json=metadata_json,
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('Создан платеж cisPay', order_id=order_id, user_id=user_id)
    return payment


async def get_cispay_payment_by_order_id(db: AsyncSession, order_id: str) -> CisPayPayment | None:
    """Получает платеж по order_id (internal)."""
    result = await db.execute(select(CisPayPayment).where(CisPayPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_cispay_payment_by_invoice_id(db: AsyncSession, cispay_payment_id: str) -> CisPayPayment | None:
    """Получает платёж по id транзакции, выданному cisPay."""
    result = await db.execute(select(CisPayPayment).where(CisPayPayment.cispay_payment_id == cispay_payment_id))
    return result.scalar_one_or_none()


async def get_cispay_payment_by_id(db: AsyncSession, payment_id: int) -> CisPayPayment | None:
    """Получает платеж по локальному ID."""
    result = await db.execute(select(CisPayPayment).where(CisPayPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_cispay_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> CisPayPayment | None:
    """Получает платёж с блокировкой FOR UPDATE."""
    result = await db.execute(
        select(CisPayPayment)
        .where(CisPayPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def update_cispay_payment_status(
    db: AsyncSession,
    payment: CisPayPayment,
    *,
    status: str,
    is_paid: bool | None = None,
    cispay_payment_id: str | None = None,
    payment_method: str | None = None,
    charged_amount_kopeks: int | None = None,
    callback_payload: dict | None = None,
    transaction_id: int | None = None,
) -> CisPayPayment:
    """Обновляет статус платежа."""
    payment.status = status
    payment.updated_at = datetime.now(UTC)

    if is_paid is not None:
        payment.is_paid = is_paid
        if is_paid:
            payment.paid_at = datetime.now(UTC)
    if cispay_payment_id is not None:
        payment.cispay_payment_id = cispay_payment_id
    if payment_method is not None:
        payment.payment_method = payment_method
    if charged_amount_kopeks is not None:
        payment.charged_amount_kopeks = charged_amount_kopeks
    if callback_payload is not None:
        payment.callback_payload = callback_payload
    if transaction_id is not None:
        payment.transaction_id = transaction_id

    await db.commit()
    await db.refresh(payment)
    logger.info(
        'Обновлён статус платежа cisPay',
        order_id=payment.order_id,
        status=status,
        is_paid=payment.is_paid,
    )
    return payment


async def get_pending_cispay_payments(db: AsyncSession, user_id: int) -> list[CisPayPayment]:
    """Возвращает незавершённые платежи пользователя."""
    result = await db.execute(
        select(CisPayPayment).where(
            CisPayPayment.user_id == user_id,
            CisPayPayment.status == 'pending',
            CisPayPayment.is_paid == False,
        )
    )
    return list(result.scalars().all())


async def get_expired_pending_cispay_payments(db: AsyncSession) -> list[CisPayPayment]:
    """Возвращает просроченные платежи в статусе pending."""
    now = datetime.now(UTC)
    result = await db.execute(
        select(CisPayPayment).where(
            CisPayPayment.status == 'pending',
            CisPayPayment.is_paid == False,
            CisPayPayment.expires_at < now,
        )
    )
    return list(result.scalars().all())


async def link_cispay_payment_to_transaction(
    db: AsyncSession,
    *,
    payment: CisPayPayment,
    transaction_id: int,
) -> CisPayPayment:
    """Связывает платёж с транзакцией."""
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)
    return payment
