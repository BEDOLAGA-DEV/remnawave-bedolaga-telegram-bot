"""CRUD operations for CloudPayments payments."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CloudPaymentsPayment


logger = structlog.get_logger(__name__)


async def create_cloudpayments_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    invoice_id: str,
    amount_kopeks: int,
    description: str | None = None,
    currency: str = 'RUB',
    payment_url: str | None = None,
    email: str | None = None,
    metadata: dict[str, Any] | None = None,
    test_mode: bool = False,
) -> CloudPaymentsPayment:
    """
    Create a new CloudPayments payment record.

    Args:
        db: Database session
        user_id: Internal user ID
        invoice_id: Unique invoice ID
        amount_kopeks: Amount in kopeks
        description: Payment description
        currency: Currency code (default RUB)
        payment_url: Payment widget URL
        email: User's email
        metadata: Additional metadata
        test_mode: Whether this is a test payment

    Returns:
        Created CloudPaymentsPayment object
    """
    payment = CloudPaymentsPayment(
        user_id=user_id,
        invoice_id=invoice_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        status='pending',
        is_paid=False,
        payment_url=payment_url,
        email=email,
        metadata_json=metadata,
        test_mode=test_mode,
    )

    db.add(payment)
    await db.flush()
    await db.refresh(payment)

    logger.debug(
        'Created CloudPayments payment: id invoice amount',
        payment_id=payment.id,
        invoice_id=invoice_id,
        amount_kopeks=amount_kopeks,
    )

    return payment


async def get_cloudpayments_payment_by_invoice_id(
    db: AsyncSession,
    invoice_id: str,
) -> CloudPaymentsPayment | None:
    """Get CloudPayments payment by invoice ID."""
    result = await db.execute(select(CloudPaymentsPayment).where(CloudPaymentsPayment.invoice_id == invoice_id))
    return result.scalars().first()


async def get_cloudpayments_payment_by_id(
    db: AsyncSession,
    payment_id: int,
) -> CloudPaymentsPayment | None:
    """Get CloudPayments payment by internal ID."""
    result = await db.execute(select(CloudPaymentsPayment).where(CloudPaymentsPayment.id == payment_id))
    return result.scalars().first()


async def get_cloudpayments_payment_by_id_for_update(
    db: AsyncSession,
    payment_id: int,
) -> CloudPaymentsPayment | None:
    result = await db.execute(
        select(CloudPaymentsPayment).where(CloudPaymentsPayment.id == payment_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def get_cloudpayments_payment_by_transaction_id(
    db: AsyncSession,
    transaction_id_cp: int,
) -> CloudPaymentsPayment | None:
    """Get CloudPayments payment by CloudPayments transaction ID."""
    result = await db.execute(
        select(CloudPaymentsPayment).where(CloudPaymentsPayment.transaction_id_cp == transaction_id_cp)
    )
    return result.scalars().first()


async def update_cloudpayments_payment(
    db: AsyncSession,
    payment_id: int,
    **kwargs: Any,
) -> CloudPaymentsPayment | None:
    """
    Update CloudPayments payment record.

    Args:
        db: Database session
        payment_id: Internal payment ID
        **kwargs: Fields to update

    Returns:
        Updated payment or None if not found
    """
    payment = await get_cloudpayments_payment_by_id(db, payment_id)
    if not payment:
        return None

    for key, value in kwargs.items():
        if hasattr(payment, key):
            setattr(payment, key, value)

    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)

    return payment


async def mark_cloudpayments_payment_as_paid(
    db: AsyncSession,
    payment_id: int,
    *,
    transaction_id_cp: int | None = None,
    token: str | None = None,
    card_first_six: str | None = None,
    card_last_four: str | None = None,
    card_type: str | None = None,
    card_exp_date: str | None = None,
    email: str | None = None,
    callback_payload: dict[str, Any] | None = None,
) -> CloudPaymentsPayment | None:
    """
    Mark CloudPayments payment as paid.

    Args:
        db: Database session
        payment_id: Internal payment ID
        transaction_id_cp: CloudPayments transaction ID
        token: Card token for recurrent payments
        card_first_six: First 6 digits of card
        card_last_four: Last 4 digits of card
        card_type: Card type (Visa, MasterCard, etc.)
        card_exp_date: Card expiration date
        email: Payer's email
        callback_payload: Full webhook payload

    Returns:
        Updated payment or None if not found
    """
    payment = await get_cloudpayments_payment_by_id(db, payment_id)
    if not payment:
        return None

    payment.status = 'completed'
    payment.is_paid = True
    payment.paid_at = datetime.now(UTC)

    if transaction_id_cp is not None:
        payment.transaction_id_cp = transaction_id_cp
    if token:
        payment.token = token
    if card_first_six:
        payment.card_first_six = card_first_six
    if card_last_four:
        payment.card_last_four = card_last_four
    if card_type:
        payment.card_type = card_type
    if card_exp_date:
        payment.card_exp_date = card_exp_date
    if email:
        payment.email = email
    if callback_payload:
        payment.callback_payload = callback_payload

    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)

    logger.info(
        'Marked CloudPayments payment as paid: id invoice', payment_id=payment.id, invoice_id=payment.invoice_id
    )

    return payment


async def link_cloudpayments_payment_to_transaction(
    db: AsyncSession,
    payment_id: int,
    transaction_id: int,
) -> CloudPaymentsPayment | None:
    """Link CloudPayments payment to internal transaction."""
    payment = await get_cloudpayments_payment_by_id(db, payment_id)
    if not payment:
        return None

    payment.transaction_id = transaction_id
    await db.flush()
    await db.refresh(payment)

    return payment


async def get_user_cloudpayments_payments(
    db: AsyncSession,
    user_id: int,
    *,
    limit: int = 10,
    offset: int = 0,
) -> list[CloudPaymentsPayment]:
    """Get CloudPayments payments for a user."""
    result = await db.execute(
        select(CloudPaymentsPayment)
        .where(CloudPaymentsPayment.user_id == user_id)
        .order_by(CloudPaymentsPayment.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())
