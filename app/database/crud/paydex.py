"""CRUD операции для платежей Paydex (paydex.pro)."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PaydexPayment


logger = structlog.get_logger(__name__)


async def create_paydex_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'RUB',
    description: str | None = None,
    payment_url: str | None = None,
    payment_method: str | None = None,
    paydex_invoice_id: str | None = None,
    charged_amount_kopeks: int | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> PaydexPayment:
    """Создаёт запись о платеже Paydex."""
    payment = PaydexPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        payment_url=payment_url,
        payment_method=payment_method,
        paydex_invoice_id=paydex_invoice_id,
        charged_amount_kopeks=charged_amount_kopeks,
        expires_at=expires_at,
        metadata_json=metadata_json,
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('Создан платеж Paydex', order_id=order_id, user_id=user_id)
    return payment


async def get_paydex_payment_by_order_id(db: AsyncSession, order_id: str) -> PaydexPayment | None:
    """Получает платёж по нашему order_id."""
    result = await db.execute(select(PaydexPayment).where(PaydexPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_paydex_payment_by_invoice_id(db: AsyncSession, paydex_invoice_id: str) -> PaydexPayment | None:
    """Получает платёж по id счёта, выданному Paydex."""
    result = await db.execute(select(PaydexPayment).where(PaydexPayment.paydex_invoice_id == paydex_invoice_id))
    return result.scalar_one_or_none()


async def get_paydex_payment_by_id(db: AsyncSession, payment_id: int) -> PaydexPayment | None:
    """Получает платеж по локальному ID."""
    result = await db.execute(select(PaydexPayment).where(PaydexPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_paydex_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> PaydexPayment | None:
    """Получает платёж с блокировкой FOR UPDATE.

    Вебхук Paydex и опрос статуса могут прийти одновременно: без блокировки
    баланс был бы начислен дважды.
    """
    result = await db.execute(
        select(PaydexPayment)
        .where(PaydexPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def update_paydex_payment_status(
    db: AsyncSession,
    payment: PaydexPayment,
    *,
    status: str,
    is_paid: bool | None = None,
    paydex_invoice_id: str | None = None,
    payment_method: str | None = None,
    charged_amount_kopeks: int | None = None,
    callback_payload: dict | None = None,
    transaction_id: int | None = None,
) -> PaydexPayment:
    """Обновляет статус платежа."""
    payment.status = status
    payment.updated_at = datetime.now(UTC)

    if is_paid is not None:
        payment.is_paid = is_paid
        if is_paid:
            payment.paid_at = datetime.now(UTC)
    if paydex_invoice_id is not None:
        payment.paydex_invoice_id = paydex_invoice_id
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
        'Обновлён статус платежа Paydex',
        order_id=payment.order_id,
        status=status,
        is_paid=payment.is_paid,
    )
    return payment


async def get_pending_paydex_payments(db: AsyncSession, user_id: int) -> list[PaydexPayment]:
    """Возвращает незавершённые платежи пользователя."""
    result = await db.execute(
        select(PaydexPayment).where(
            PaydexPayment.user_id == user_id,
            PaydexPayment.status == 'pending',
            PaydexPayment.is_paid == False,
        )
    )
    return list(result.scalars().all())


async def get_expired_pending_paydex_payments(db: AsyncSession) -> list[PaydexPayment]:
    """Возвращает просроченные платежи в статусе pending."""
    now = datetime.now(UTC)
    result = await db.execute(
        select(PaydexPayment).where(
            PaydexPayment.status == 'pending',
            PaydexPayment.is_paid == False,
            PaydexPayment.expires_at < now,
        )
    )
    return list(result.scalars().all())


async def link_paydex_payment_to_transaction(
    db: AsyncSession,
    *,
    payment: PaydexPayment,
    transaction_id: int,
) -> PaydexPayment:
    """Связывает платёж с транзакцией."""
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)
    return payment
