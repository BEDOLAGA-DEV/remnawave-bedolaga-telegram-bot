"""CRUD helpers for PayPalych (Pal24) payments."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Pal24Payment


logger = structlog.get_logger(__name__)


async def create_pal24_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    bill_id: str,
    amount_kopeks: int,
    description: str | None,
    status: str,
    type_: str,
    currency: str,
    link_url: str | None,
    link_page_url: str | None,
    order_id: str | None = None,
    ttl: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> Pal24Payment:
    payment = Pal24Payment(
        user_id=user_id,
        bill_id=bill_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        status=status,
        type=type_,
        link_url=link_url,
        link_page_url=link_page_url,
        metadata_json=metadata or {},
        ttl=ttl,
    )

    db.add(payment)
    await db.commit()
    await db.refresh(payment)

    logger.info(
        'Создан Pal24 платеж # для пользователя : копеек (статус)',
        payment_id=payment.id,
        user_id=user_id,
        amount_kopeks=amount_kopeks,
        status=status,
    )

    return payment


async def get_pal24_payment_by_id(db: AsyncSession, payment_id: int) -> Pal24Payment | None:
    result = await db.execute(select(Pal24Payment).where(Pal24Payment.id == payment_id))
    return result.scalar_one_or_none()


async def get_pal24_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> Pal24Payment | None:
    result = await db.execute(
        select(Pal24Payment)
        .where(Pal24Payment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_pal24_payment_by_bill_id(db: AsyncSession, bill_id: str) -> Pal24Payment | None:
    result = await db.execute(select(Pal24Payment).where(Pal24Payment.bill_id == bill_id))
    return result.scalar_one_or_none()


async def get_pal24_payment_by_order_id(db: AsyncSession, order_id: str) -> Pal24Payment | None:
    result = await db.execute(select(Pal24Payment).where(Pal24Payment.order_id == order_id))
    return result.scalar_one_or_none()


async def update_pal24_payment_status(
    db: AsyncSession,
    payment: Pal24Payment,
    *,
    status: str,
    is_active: bool | None = None,
    is_paid: bool | None = None,
    paid_at: datetime | None = None,
    payment_id: str | None = None,
    payment_status: str | None = None,
    payment_method: str | None = None,
    balance_amount: str | None = None,
    balance_currency: str | None = None,
    payer_account: str | None = None,
    callback_payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Pal24Payment:
    update_values: dict[str, Any] = {
        'status': status,
    }

    if is_active is not None:
        update_values['is_active'] = is_active
    if is_paid is not None:
        update_values['is_paid'] = is_paid
    if paid_at is not None:
        update_values['paid_at'] = paid_at
    if payment_id is not None:
        update_values['payment_id'] = payment_id
    if payment_status is not None:
        update_values['payment_status'] = payment_status
    if payment_method is not None:
        update_values['payment_method'] = payment_method
    if balance_amount is not None:
        update_values['balance_amount'] = balance_amount
    if balance_currency is not None:
        update_values['balance_currency'] = balance_currency
    if payer_account is not None:
        update_values['payer_account'] = payer_account
    if callback_payload is not None:
        update_values['callback_payload'] = callback_payload
    if metadata is not None:
        update_values['metadata_json'] = metadata

    update_values['last_status'] = status

    await db.execute(update(Pal24Payment).where(Pal24Payment.id == payment.id).values(**update_values))

    await db.commit()
    await db.refresh(payment)

    logger.info(
        'Обновлен Pal24 платеж : статус is_paid',
        bill_id=payment.bill_id,
        payment_status=payment.status,
        is_paid=payment.is_paid,
    )

    return payment


async def link_pal24_payment_to_transaction(
    db: AsyncSession,
    payment: Pal24Payment,
    transaction_id: int,
) -> Pal24Payment:
    await db.execute(update(Pal24Payment).where(Pal24Payment.id == payment.id).values(transaction_id=transaction_id))
    await db.flush()
    await db.refresh(payment)
    logger.info('Pal24 платеж привязан к транзакции', bill_id=payment.bill_id, transaction_id=transaction_id)
    return payment
