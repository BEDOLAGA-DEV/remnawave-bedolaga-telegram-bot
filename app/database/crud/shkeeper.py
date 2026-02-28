"""CRUD операции для платежей SHKeeper."""

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ShkeeperPayment


logger = structlog.get_logger(__name__)


async def create_shkeeper_payment(
    db: AsyncSession,
    *,
    user_id: int,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'RUB',
    description: str | None = None,
    payment_url: str | None = None,
    shkeeper_invoice_id: str | None = None,
    external_id: str | None = None,
    amount_crypto: str | None = None,
    crypto: str | None = None,
    display_amount: str | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> ShkeeperPayment:
    payment = ShkeeperPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        payment_url=payment_url,
        shkeeper_invoice_id=shkeeper_invoice_id,
        external_id=external_id,
        amount_crypto=amount_crypto,
        crypto=crypto,
        display_amount=display_amount,
        expires_at=expires_at,
        metadata_json=metadata_json,
        status='new',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('Создан платеж SHKeeper', order_id=order_id, user_id=user_id)
    return payment


async def get_shkeeper_payment_by_id(db: AsyncSession, payment_id: int) -> ShkeeperPayment | None:
    result = await db.execute(select(ShkeeperPayment).where(ShkeeperPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_shkeeper_payment_by_order_id(db: AsyncSession, order_id: str) -> ShkeeperPayment | None:
    result = await db.execute(select(ShkeeperPayment).where(ShkeeperPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_shkeeper_payment_by_external_id(db: AsyncSession, external_id: str) -> ShkeeperPayment | None:
    result = await db.execute(select(ShkeeperPayment).where(ShkeeperPayment.external_id == external_id))
    return result.scalar_one_or_none()


async def get_shkeeper_payment_by_invoice_id(db: AsyncSession, invoice_id: str) -> ShkeeperPayment | None:
    result = await db.execute(select(ShkeeperPayment).where(ShkeeperPayment.shkeeper_invoice_id == invoice_id))
    return result.scalar_one_or_none()


async def update_shkeeper_payment_status(
    db: AsyncSession,
    payment: ShkeeperPayment,
    *,
    status: str | None = None,
    is_paid: bool | None = None,
    paid_at: datetime | None = None,
    payment_url: str | None = None,
    shkeeper_invoice_id: str | None = None,
    external_id: str | None = None,
    amount_crypto: str | None = None,
    crypto: str | None = None,
    display_amount: str | None = None,
    metadata_json: dict[str, Any] | None = None,
    callback_payload: dict[str, Any] | None = None,
    transaction_id: int | None = None,
) -> ShkeeperPayment:
    values: dict[str, Any] = {'updated_at': datetime.now(UTC)}
    if status is not None:
        values['status'] = status
    if is_paid is not None:
        values['is_paid'] = is_paid
    if paid_at is not None:
        values['paid_at'] = paid_at
    if payment_url is not None:
        values['payment_url'] = payment_url
    if shkeeper_invoice_id is not None:
        values['shkeeper_invoice_id'] = shkeeper_invoice_id
    if external_id is not None:
        values['external_id'] = external_id
    if amount_crypto is not None:
        values['amount_crypto'] = amount_crypto
    if crypto is not None:
        values['crypto'] = crypto
    if display_amount is not None:
        values['display_amount'] = display_amount
    if metadata_json is not None:
        values['metadata_json'] = metadata_json
    if callback_payload is not None:
        values['callback_payload'] = callback_payload
    if transaction_id is not None:
        values['transaction_id'] = transaction_id

    await db.execute(update(ShkeeperPayment).where(ShkeeperPayment.id == payment.id).values(**values))
    await db.commit()
    await db.refresh(payment)
    logger.info('Обновлен платеж SHKeeper', order_id=payment.order_id, status=payment.status, is_paid=payment.is_paid)
    return payment
