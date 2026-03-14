"""CRUD helpers for WATA payment records."""

from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import WataPayment


logger = structlog.get_logger(__name__)


async def create_wata_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    payment_link_id: str,
    amount_kopeks: int,
    currency: str,
    description: str | None,
    status: str,
    type_: str | None,
    url: str | None,
    order_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
    terminal_public_id: str | None = None,
    success_redirect_url: str | None = None,
    fail_redirect_url: str | None = None,
) -> WataPayment:
    payment = WataPayment(
        user_id=user_id,
        payment_link_id=payment_link_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        status=status,
        type=type_,
        url=url,
        metadata_json=metadata or {},
        expires_at=expires_at,
        terminal_public_id=terminal_public_id,
        success_redirect_url=success_redirect_url,
        fail_redirect_url=fail_redirect_url,
    )

    db.add(payment)
    await db.commit()
    await db.refresh(payment)

    logger.info(
        'Создан Wata платеж # для пользователя : копеек (статус)',
        payment_id=payment.id,
        user_id=user_id,
        amount_kopeks=amount_kopeks,
        status=status,
    )

    return payment


async def get_wata_payment_by_id(
    db: AsyncSession,
    payment_id: int,
) -> WataPayment | None:
    result = await db.execute(select(WataPayment).where(WataPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_wata_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> WataPayment | None:
    result = await db.execute(select(WataPayment).where(WataPayment.id == payment_id).with_for_update())
    return result.scalar_one_or_none()


async def get_wata_payment_by_link_id(
    db: AsyncSession,
    payment_link_id: str,
) -> WataPayment | None:
    result = await db.execute(select(WataPayment).where(WataPayment.payment_link_id == payment_link_id))
    return result.scalar_one_or_none()


async def get_wata_payment_by_order_id(
    db: AsyncSession,
    order_id: str,
) -> WataPayment | None:
    result = await db.execute(select(WataPayment).where(WataPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def update_wata_payment_status(
    db: AsyncSession,
    payment: WataPayment,
    *,
    status: str | None = None,
    is_paid: bool | None = None,
    paid_at: datetime | None = None,
    last_status: str | None = None,
    url: str | None = None,
    metadata: dict[str, Any] | None = None,
    callback_payload: dict[str, Any] | None = None,
    terminal_public_id: str | None = None,
) -> WataPayment:
    update_values: dict[str, Any] = {}

    if status is not None:
        update_values['status'] = status
    if is_paid is not None:
        update_values['is_paid'] = is_paid
    if paid_at is not None:
        update_values['paid_at'] = paid_at
    if last_status is not None:
        update_values['last_status'] = last_status
    if url is not None:
        update_values['url'] = url
    if metadata is not None:
        update_values['metadata_json'] = metadata
    if callback_payload is not None:
        update_values['callback_payload'] = callback_payload
    if terminal_public_id is not None:
        update_values['terminal_public_id'] = terminal_public_id

    if not update_values:
        return payment

    await db.execute(update(WataPayment).where(WataPayment.id == payment.id).values(**update_values))

    await db.commit()
    await db.refresh(payment)

    logger.info(
        'Обновлен Wata платеж : статус is_paid',
        payment_link_id=payment.payment_link_id,
        payment_status=payment.status,
        is_paid=payment.is_paid,
    )

    return payment


async def link_wata_payment_to_transaction(
    db: AsyncSession,
    payment: WataPayment,
    transaction_id: int,
) -> WataPayment:
    await db.execute(update(WataPayment).where(WataPayment.id == payment.id).values(transaction_id=transaction_id))
    await db.flush()
    await db.refresh(payment)

    logger.info(
        'Wata платеж привязан к транзакции', payment_link_id=payment.payment_link_id, transaction_id=transaction_id
    )

    return payment
