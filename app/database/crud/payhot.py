"""PayHot invoice reads. Callers own financial transaction boundaries."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PayHotPayment


async def create_payhot_payment(db: AsyncSession, *, commit: bool = True, **values) -> PayHotPayment:
    payment = PayHotPayment(**values)
    db.add(payment)
    await db.flush()
    if commit:
        await db.commit()
    return payment


async def get_payhot_payment_by_order_id(db: AsyncSession, order_id: str) -> PayHotPayment | None:
    return await db.scalar(select(PayHotPayment).where(PayHotPayment.order_id == order_id))


async def get_payhot_payment_by_invoice_id(db: AsyncSession, invoice_id: str) -> PayHotPayment | None:
    return await db.scalar(select(PayHotPayment).where(PayHotPayment.payhot_payment_id == invoice_id))


async def get_payhot_payment_by_id(db: AsyncSession, payment_id: int) -> PayHotPayment | None:
    return await db.get(PayHotPayment, payment_id)


async def get_payhot_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> PayHotPayment | None:
    return await db.scalar(
        select(PayHotPayment)
        .where(PayHotPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
