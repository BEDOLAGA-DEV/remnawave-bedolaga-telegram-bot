"""CRUD операции для платежей Cashera (api.cashera.cash)."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CasheraPayment


logger = structlog.get_logger(__name__)


async def create_cashera_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'RUB',
    description: str | None = None,
    payment_url: str | None = None,
    payment_method: str | None = None,
    cashera_uuid: str | None = None,
    cashera_status: str | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> CasheraPayment:
    """Создаёт запись о платеже Cashera."""
    payment = CasheraPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        payment_url=payment_url,
        payment_method=payment_method,
        cashera_uuid=cashera_uuid,
        cashera_status=cashera_status,
        expires_at=expires_at,
        metadata_json=metadata_json,
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('Создан платеж Cashera', order_id=order_id, user_id=user_id)
    return payment


async def get_cashera_payment_by_order_id(db: AsyncSession, order_id: str) -> CasheraPayment | None:
    """Получает платеж по нашему external_id."""
    result = await db.execute(select(CasheraPayment).where(CasheraPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_cashera_payment_by_invoice_id(db: AsyncSession, cashera_uuid: str) -> CasheraPayment | None:
    """Получает платёж по uuid транзакции Cashera."""
    result = await db.execute(select(CasheraPayment).where(CasheraPayment.cashera_uuid == cashera_uuid))
    return result.scalar_one_or_none()


async def get_cashera_payment_by_id(db: AsyncSession, payment_id: int) -> CasheraPayment | None:
    """Получает платеж по локальному ID."""
    result = await db.execute(select(CasheraPayment).where(CasheraPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_cashera_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> CasheraPayment | None:
    """Получает платёж с блокировкой FOR UPDATE."""
    result = await db.execute(
        select(CasheraPayment)
        .where(CasheraPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def update_cashera_payment_status(
    db: AsyncSession,
    payment: CasheraPayment,
    *,
    status: str,
    is_paid: bool | None = None,
    cashera_uuid: str | None = None,
    cashera_status: str | None = None,
    payment_method: str | None = None,
    callback_payload: dict | None = None,
    transaction_id: int | None = None,
) -> CasheraPayment:
    """Обновляет статус платежа."""
    payment.status = status
    payment.updated_at = datetime.now(UTC)

    if is_paid is not None:
        payment.is_paid = is_paid
        if is_paid:
            payment.paid_at = datetime.now(UTC)
    if cashera_uuid is not None:
        payment.cashera_uuid = cashera_uuid
    if cashera_status is not None:
        payment.cashera_status = cashera_status
    if payment_method is not None:
        payment.payment_method = payment_method
    if callback_payload is not None:
        payment.callback_payload = callback_payload
    if transaction_id is not None:
        payment.transaction_id = transaction_id

    await db.commit()
    await db.refresh(payment)
    logger.info(
        'Обновлён статус платежа Cashera',
        order_id=payment.order_id,
        status=status,
        is_paid=payment.is_paid,
    )
    return payment


async def get_pending_cashera_payments(db: AsyncSession, user_id: int) -> list[CasheraPayment]:
    """Возвращает незавершённые платежи пользователя."""
    result = await db.execute(
        select(CasheraPayment).where(
            CasheraPayment.user_id == user_id,
            CasheraPayment.status == 'pending',
            CasheraPayment.is_paid == False,
        )
    )
    return list(result.scalars().all())


async def link_cashera_payment_to_transaction(
    db: AsyncSession,
    *,
    payment: CasheraPayment,
    transaction_id: int,
) -> CasheraPayment:
    """Связывает платёж с транзакцией."""
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)
    return payment
