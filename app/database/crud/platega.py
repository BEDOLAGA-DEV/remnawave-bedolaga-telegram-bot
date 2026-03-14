"""CRUD-операции для платежей Platega."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PlategaPayment


logger = structlog.get_logger(__name__)


async def create_platega_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    amount_kopeks: int,
    currency: str,
    description: str | None,
    status: str,
    payment_method_code: int,
    correlation_id: str,
    platega_transaction_id: str | None,
    redirect_url: str | None,
    return_url: str | None,
    failed_url: str | None,
    payload: str | None,
    metadata: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> PlategaPayment:
    payment = PlategaPayment(
        user_id=user_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        status=status,
        payment_method_code=payment_method_code,
        correlation_id=correlation_id,
        platega_transaction_id=platega_transaction_id,
        redirect_url=redirect_url,
        return_url=return_url,
        failed_url=failed_url,
        payload=payload,
        metadata_json=metadata or {},
        expires_at=expires_at,
    )

    db.add(payment)
    await db.commit()
    await db.refresh(payment)

    logger.info(
        'Создан Platega платеж # (tx=) на сумму копеек для пользователя',
        payment_id=payment.id,
        platega_transaction_id=platega_transaction_id,
        amount_kopeks=amount_kopeks,
        user_id=user_id,
    )

    return payment


async def get_platega_payment_by_id(db: AsyncSession, payment_id: int) -> PlategaPayment | None:
    result = await db.execute(select(PlategaPayment).where(PlategaPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_platega_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> PlategaPayment | None:
    result = await db.execute(select(PlategaPayment).where(PlategaPayment.id == payment_id).with_for_update())
    return result.scalar_one_or_none()


async def get_platega_payment_by_transaction_id(db: AsyncSession, transaction_id: str) -> PlategaPayment | None:
    result = await db.execute(select(PlategaPayment).where(PlategaPayment.platega_transaction_id == transaction_id))
    return result.scalar_one_or_none()


async def get_platega_payment_by_correlation_id(db: AsyncSession, correlation_id: str) -> PlategaPayment | None:
    result = await db.execute(select(PlategaPayment).where(PlategaPayment.correlation_id == correlation_id))
    return result.scalar_one_or_none()


async def update_platega_payment(
    db: AsyncSession,
    *,
    payment: PlategaPayment,
    status: str | None = None,
    is_paid: bool | None = None,
    paid_at: datetime | None = None,
    platega_transaction_id: str | None = None,
    redirect_url: str | None = None,
    callback_payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> PlategaPayment:
    if status is not None:
        payment.status = status
    if is_paid is not None:
        payment.is_paid = is_paid
    if paid_at is not None:
        payment.paid_at = paid_at
    if platega_transaction_id and not payment.platega_transaction_id:
        payment.platega_transaction_id = platega_transaction_id
    if redirect_url is not None:
        payment.redirect_url = redirect_url
    if callback_payload is not None:
        payment.callback_payload = callback_payload
    if metadata is not None:
        payment.metadata_json = metadata
    if expires_at is not None:
        payment.expires_at = expires_at

    payment.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(payment)
    return payment


async def link_platega_payment_to_transaction(
    db: AsyncSession,
    *,
    payment: PlategaPayment,
    transaction_id: int,
) -> PlategaPayment:
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)
    return payment
