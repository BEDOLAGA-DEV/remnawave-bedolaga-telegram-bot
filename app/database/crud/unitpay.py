"""CRUD операции для платежей UnitPay."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import UnitPayPayment


logger = structlog.get_logger(__name__)


async def create_unitpay_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'RUB',
    description: str | None = None,
    payment_url: str | None = None,
    payment_type: str | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> UnitPayPayment:
    """Создает запись о платеже UnitPay."""
    payment = UnitPayPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        payment_url=payment_url,
        payment_type=payment_type,
        expires_at=expires_at,
        metadata_json=metadata_json,
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('Создан платеж UnitPay', order_id=order_id, user_id=user_id)
    return payment


async def get_unitpay_payment_by_order_id(db: AsyncSession, order_id: str) -> UnitPayPayment | None:
    """Получает платеж по order_id (account)."""
    result = await db.execute(select(UnitPayPayment).where(UnitPayPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_unitpay_payment_by_unitpay_id(db: AsyncSession, unitpay_id: int) -> UnitPayPayment | None:
    """Получает платеж по ID от UnitPay."""
    result = await db.execute(select(UnitPayPayment).where(UnitPayPayment.unitpay_payment_id == unitpay_id))
    return result.scalar_one_or_none()


async def get_unitpay_payment_by_id(db: AsyncSession, payment_id: int) -> UnitPayPayment | None:
    """Получает платеж по локальному ID."""
    result = await db.execute(select(UnitPayPayment).where(UnitPayPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_unitpay_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> UnitPayPayment | None:
    """Получает платеж с блокировкой строки (FOR UPDATE)."""
    result = await db.execute(
        select(UnitPayPayment)
        .where(UnitPayPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_pending_unitpay_payments(db: AsyncSession, user_id: int) -> list[UnitPayPayment]:
    """Получает незавершенные платежи пользователя."""
    result = await db.execute(
        select(UnitPayPayment).where(
            UnitPayPayment.user_id == user_id,
            UnitPayPayment.status == 'pending',
            UnitPayPayment.is_paid == False,  # noqa: E712
        )
    )
    return list(result.scalars().all())


async def get_expired_pending_unitpay_payments(db: AsyncSession) -> list[UnitPayPayment]:
    """Получает просроченные платежи в статусе pending."""
    now = datetime.now(UTC)
    result = await db.execute(
        select(UnitPayPayment).where(
            UnitPayPayment.status == 'pending',
            UnitPayPayment.is_paid == False,  # noqa: E712
            UnitPayPayment.expires_at < now,
        )
    )
    return list(result.scalars().all())
