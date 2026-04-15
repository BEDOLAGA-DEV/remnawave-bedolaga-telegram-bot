from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import RobokassaPayment


logger = structlog.get_logger(__name__)


async def create_robokassa_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    amount_kopeks: int,
    inv_id: int,
    description: str,
    payment_url: str | None,
    currency: str = 'RUB',
    status: str = 'created',
    inc_curr_label: str | None = None,
    metadata: dict | None = None,
) -> RobokassaPayment:
    payment = RobokassaPayment(
        user_id=user_id,
        amount_kopeks=amount_kopeks,
        inv_id=inv_id,
        description=description,
        payment_url=payment_url,
        currency=currency,
        status=status,
        inc_curr_label=inc_curr_label,
        metadata_json=metadata or {},
    )

    db.add(payment)
    await db.commit()
    await db.refresh(payment)

    logger.info(
        'Создан платеж Robokassa',
        display_name=settings.get_robokassa_display_name(),
        inv_id=inv_id,
        amount_kopeks=amount_kopeks,
        user_id=user_id,
    )

    return payment


async def get_robokassa_payment_by_local_id(db: AsyncSession, payment_id: int) -> RobokassaPayment | None:
    result = await db.execute(select(RobokassaPayment).where(RobokassaPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_robokassa_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> RobokassaPayment | None:
    result = await db.execute(
        select(RobokassaPayment)
        .where(RobokassaPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_robokassa_payment_by_inv_id(db: AsyncSession, inv_id: int) -> RobokassaPayment | None:
    result = await db.execute(select(RobokassaPayment).where(RobokassaPayment.inv_id == inv_id))
    return result.scalar_one_or_none()


async def get_max_robokassa_inv_id(db: AsyncSession) -> int:
    result = await db.execute(select(RobokassaPayment.inv_id).order_by(RobokassaPayment.inv_id.desc()).limit(1))
    value = result.scalar_one_or_none()
    return int(value or 0)


async def update_robokassa_payment_status(
    db: AsyncSession,
    *,
    payment: RobokassaPayment,
    status: str,
    is_paid: bool | None = None,
    paid_at: datetime | None = None,
    callback_payload: dict | None = None,
    robokassa_op_id: str | None = None,
    metadata: dict | None = None,
) -> RobokassaPayment:
    payment.status = status
    if is_paid is not None:
        payment.is_paid = is_paid
    if paid_at:
        payment.paid_at = paid_at
    if callback_payload is not None:
        payment.callback_payload = callback_payload
    if robokassa_op_id is not None and not payment.robokassa_op_id:
        payment.robokassa_op_id = robokassa_op_id
    if metadata is not None:
        payment.metadata_json = metadata

    payment.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(payment)
    return payment


async def update_robokassa_payment_metadata(
    db: AsyncSession,
    *,
    payment: RobokassaPayment,
    metadata: dict,
) -> RobokassaPayment:
    payment.metadata_json = metadata
    payment.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(payment)
    return payment


async def link_robokassa_payment_to_transaction(
    db: AsyncSession,
    *,
    payment: RobokassaPayment,
    transaction_id: int,
) -> RobokassaPayment:
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)
    return payment
