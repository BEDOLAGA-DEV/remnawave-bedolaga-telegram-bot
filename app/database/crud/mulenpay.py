from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import MulenPayPayment


logger = structlog.get_logger(__name__)


async def create_mulenpay_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    amount_kopeks: int,
    uuid: str,
    description: str,
    payment_url: str | None,
    mulen_payment_id: int | None,
    currency: str,
    status: str,
    metadata: dict | None = None,
) -> MulenPayPayment:
    payment = MulenPayPayment(
        user_id=user_id,
        amount_kopeks=amount_kopeks,
        uuid=uuid,
        description=description,
        payment_url=payment_url,
        mulen_payment_id=mulen_payment_id,
        currency=currency,
        status=status,
        metadata_json=metadata or {},
    )

    db.add(payment)
    await db.commit()
    await db.refresh(payment)

    logger.info(
        'Создан платеж # (uuid=) на сумму копеек для пользователя',
        get_mulenpay_display_name=settings.get_mulenpay_display_name(),
        mulen_payment_id=payment.mulen_payment_id,
        uuid=uuid,
        amount_kopeks=amount_kopeks,
        user_id=user_id,
    )

    return payment


async def get_mulenpay_payment_by_local_id(db: AsyncSession, payment_id: int) -> MulenPayPayment | None:
    result = await db.execute(select(MulenPayPayment).where(MulenPayPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_mulenpay_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> MulenPayPayment | None:
    result = await db.execute(
        select(MulenPayPayment)
        .where(MulenPayPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_mulenpay_payment_by_uuid(db: AsyncSession, uuid: str) -> MulenPayPayment | None:
    result = await db.execute(select(MulenPayPayment).where(MulenPayPayment.uuid == uuid))
    return result.scalar_one_or_none()


async def get_mulenpay_payment_by_mulen_id(db: AsyncSession, mulen_payment_id: int) -> MulenPayPayment | None:
    result = await db.execute(select(MulenPayPayment).where(MulenPayPayment.mulen_payment_id == mulen_payment_id))
    return result.scalar_one_or_none()


async def update_mulenpay_payment_status(
    db: AsyncSession,
    *,
    payment: MulenPayPayment,
    status: str,
    is_paid: bool | None = None,
    paid_at: datetime | None = None,
    callback_payload: dict | None = None,
    mulen_payment_id: int | None = None,
    metadata: dict | None = None,
) -> MulenPayPayment:
    payment.status = status
    if is_paid is not None:
        payment.is_paid = is_paid
    if paid_at:
        payment.paid_at = paid_at
    if callback_payload is not None:
        payment.callback_payload = callback_payload
    if mulen_payment_id is not None and not payment.mulen_payment_id:
        payment.mulen_payment_id = mulen_payment_id
    if metadata is not None:
        payment.metadata_json = metadata

    payment.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(payment)
    return payment


async def update_mulenpay_payment_metadata(
    db: AsyncSession,
    *,
    payment: MulenPayPayment,
    metadata: dict,
) -> MulenPayPayment:
    payment.metadata_json = metadata
    payment.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(payment)
    return payment


async def link_mulenpay_payment_to_transaction(
    db: AsyncSession,
    *,
    payment: MulenPayPayment,
    transaction_id: int,
) -> MulenPayPayment:
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)
    return payment
