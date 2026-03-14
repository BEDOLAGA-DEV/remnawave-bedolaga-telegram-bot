"""CRUD операции для платежей Freekassa."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import FreekassaPayment


logger = structlog.get_logger(__name__)


async def create_freekassa_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'RUB',
    description: str | None = None,
    payment_url: str | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> FreekassaPayment:
    """Создает запись о платеже Freekassa."""
    payment = FreekassaPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        payment_url=payment_url,
        expires_at=expires_at,
        metadata_json=metadata_json,
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('Создан платеж Freekassa: order_id=, user_id', order_id=order_id, user_id=user_id)
    return payment


async def get_freekassa_payment_by_order_id(db: AsyncSession, order_id: str) -> FreekassaPayment | None:
    """Получает платеж по order_id."""
    result = await db.execute(select(FreekassaPayment).where(FreekassaPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_freekassa_payment_by_fk_order_id(db: AsyncSession, freekassa_order_id: str) -> FreekassaPayment | None:
    """Получает платеж по ID от Freekassa (intid)."""
    result = await db.execute(select(FreekassaPayment).where(FreekassaPayment.freekassa_order_id == freekassa_order_id))
    return result.scalar_one_or_none()


async def get_freekassa_payment_by_id(db: AsyncSession, payment_id: int) -> FreekassaPayment | None:
    """Получает платеж по ID."""
    result = await db.execute(select(FreekassaPayment).where(FreekassaPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_freekassa_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> FreekassaPayment | None:
    result = await db.execute(select(FreekassaPayment).where(FreekassaPayment.id == payment_id).with_for_update())
    return result.scalar_one_or_none()


async def update_freekassa_payment_status(
    db: AsyncSession,
    payment: FreekassaPayment,
    *,
    status: str,
    is_paid: bool = False,
    freekassa_order_id: str | None = None,
    payment_system_id: int | None = None,
    callback_payload: dict | None = None,
    transaction_id: int | None = None,
) -> FreekassaPayment:
    """Обновляет статус платежа."""
    payment.status = status
    payment.is_paid = is_paid
    payment.updated_at = datetime.now(UTC)

    if is_paid:
        payment.paid_at = datetime.now(UTC)
    if freekassa_order_id:
        payment.freekassa_order_id = freekassa_order_id
    if payment_system_id is not None:
        payment.payment_system_id = payment_system_id
    if callback_payload:
        payment.callback_payload = callback_payload
    if transaction_id:
        payment.transaction_id = transaction_id

    await db.commit()
    await db.refresh(payment)
    logger.info(
        'Обновлен статус платежа Freekassa: order_id=, status=, is_paid',
        order_id=payment.order_id,
        status=status,
        is_paid=is_paid,
    )
    return payment


async def get_pending_freekassa_payments(db: AsyncSession, user_id: int) -> list[FreekassaPayment]:
    """Получает незавершенные платежи пользователя."""
    result = await db.execute(
        select(FreekassaPayment).where(
            FreekassaPayment.user_id == user_id,
            FreekassaPayment.status == 'pending',
            FreekassaPayment.is_paid == False,
        )
    )
    return list(result.scalars().all())


async def get_user_freekassa_payments(
    db: AsyncSession,
    user_id: int,
    limit: int = 10,
    offset: int = 0,
) -> list[FreekassaPayment]:
    """Получает платежи пользователя с пагинацией."""
    result = await db.execute(
        select(FreekassaPayment)
        .where(FreekassaPayment.user_id == user_id)
        .order_by(FreekassaPayment.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def get_expired_pending_payments(
    db: AsyncSession,
) -> list[FreekassaPayment]:
    """Получает просроченные платежи в статусе pending."""
    now = datetime.now(UTC)
    result = await db.execute(
        select(FreekassaPayment).where(
            FreekassaPayment.status == 'pending',
            FreekassaPayment.is_paid == False,
            FreekassaPayment.expires_at < now,
        )
    )
    return list(result.scalars().all())
