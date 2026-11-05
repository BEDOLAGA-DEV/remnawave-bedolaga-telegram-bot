"""Anore invoice reads. Callers own financial transaction boundaries."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AnorePayment


async def create_anore_payment(db: AsyncSession, *, commit: bool = True, **values) -> AnorePayment:
    payment = AnorePayment(**values)
    db.add(payment)
    await db.flush()
    if commit:
        await db.commit()
    return payment


async def get_anore_payment_by_order_id(db: AsyncSession, order_id: str) -> AnorePayment | None:
    return await db.scalar(select(AnorePayment).where(AnorePayment.order_id == order_id))


async def get_anore_payment_by_invoice_id(db: AsyncSession, invoice_id: str) -> AnorePayment | None:
    return await db.scalar(select(AnorePayment).where(AnorePayment.anore_payment_id == invoice_id))


async def get_anore_payment_by_id(db: AsyncSession, payment_id: int) -> AnorePayment | None:
    return await db.get(AnorePayment, payment_id)


async def get_anore_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> AnorePayment | None:
    return await db.scalar(
        select(AnorePayment)
        .where(AnorePayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
