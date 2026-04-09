from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import CryptoBotPayment


logger = structlog.get_logger(__name__)


async def create_cryptobot_payment(
    db: AsyncSession,
    user_id: int | None,
    invoice_id: str,
    amount: str,
    asset: str,
    status: str = 'active',
    description: str | None = None,
    payload: str | None = None,
    bot_invoice_url: str | None = None,
    mini_app_invoice_url: str | None = None,
    web_app_invoice_url: str | None = None,
) -> CryptoBotPayment:
    payment = CryptoBotPayment(
        user_id=user_id,
        invoice_id=invoice_id,
        amount=amount,
        asset=asset,
        status=status,
        description=description,
        payload=payload,
        bot_invoice_url=bot_invoice_url,
        mini_app_invoice_url=mini_app_invoice_url,
        web_app_invoice_url=web_app_invoice_url,
    )

    db.add(payment)
    await db.commit()
    await db.refresh(payment)

    logger.info(
        'Создан CryptoBot платеж: на для пользователя',
        invoice_id=invoice_id,
        amount=amount,
        asset=asset,
        user_id=user_id,
    )
    return payment


async def get_cryptobot_payment_by_invoice_id(db: AsyncSession, invoice_id: str) -> CryptoBotPayment | None:
    result = await db.execute(
        select(CryptoBotPayment)
        .options(selectinload(CryptoBotPayment.user))
        .where(CryptoBotPayment.invoice_id == invoice_id)
    )
    return result.scalar_one_or_none()


async def get_cryptobot_payment_by_id(db: AsyncSession, payment_id: int) -> CryptoBotPayment | None:
    result = await db.execute(
        select(CryptoBotPayment).options(selectinload(CryptoBotPayment.user)).where(CryptoBotPayment.id == payment_id)
    )
    return result.scalar_one_or_none()


async def get_cryptobot_payment_by_invoice_id_for_update(db: AsyncSession, invoice_id: str) -> CryptoBotPayment | None:
    result = await db.execute(
        select(CryptoBotPayment)
        .options(selectinload(CryptoBotPayment.user))
        .where(CryptoBotPayment.invoice_id == invoice_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_cryptobot_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> CryptoBotPayment | None:
    result = await db.execute(
        select(CryptoBotPayment)
        .where(CryptoBotPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def update_cryptobot_payment_status(
    db: AsyncSession,
    invoice_id: str,
    status: str,
    paid_at: datetime | None = None,
    *,
    commit: bool = True,
) -> CryptoBotPayment | None:
    payment = await get_cryptobot_payment_by_invoice_id(db, invoice_id)

    if not payment:
        return None

    payment.status = status
    payment.updated_at = datetime.now(UTC)

    if status == 'paid' and paid_at:
        payment.paid_at = paid_at

    if commit:
        await db.commit()
        await db.refresh(payment)
    else:
        await db.flush()

    logger.info('Обновлен статус CryptoBot платежа', invoice_id=invoice_id, status=status)
    return payment


async def link_cryptobot_payment_to_transaction(
    db: AsyncSession, invoice_id: str, transaction_id: int
) -> CryptoBotPayment | None:
    payment = await get_cryptobot_payment_by_invoice_id(db, invoice_id)

    if not payment:
        return None

    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)

    await db.flush()
    await db.refresh(payment)

    logger.info('Связан CryptoBot платеж с транзакцией', invoice_id=invoice_id, transaction_id=transaction_id)
    return payment


async def get_user_cryptobot_payments(
    db: AsyncSession, user_id: int, limit: int = 50, offset: int = 0
) -> list[CryptoBotPayment]:
    result = await db.execute(
        select(CryptoBotPayment)
        .where(CryptoBotPayment.user_id == user_id)
        .order_by(CryptoBotPayment.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return result.scalars().all()


async def get_pending_cryptobot_payments(db: AsyncSession, older_than_hours: int = 24) -> list[CryptoBotPayment]:
    cutoff_time = datetime.now(UTC) - timedelta(hours=older_than_hours)

    result = await db.execute(
        select(CryptoBotPayment)
        .options(selectinload(CryptoBotPayment.user))
        .where(and_(CryptoBotPayment.status == 'active', CryptoBotPayment.created_at < cutoff_time))
        .order_by(CryptoBotPayment.created_at)
    )
    return result.scalars().all()
