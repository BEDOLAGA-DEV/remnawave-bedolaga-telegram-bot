from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AppleTransaction


logger = structlog.get_logger(__name__)


async def create_apple_transaction(
    db: AsyncSession,
    user_id: int,
    transaction_id: str,
    product_id: str,
    bundle_id: str,
    amount_kopeks: int,
    environment: str,
    original_transaction_id: str | None = None,
    transaction_id_fk: int | None = None,
) -> AppleTransaction:
    apple_txn = AppleTransaction(
        user_id=user_id,
        transaction_id=transaction_id,
        original_transaction_id=original_transaction_id,
        product_id=product_id,
        bundle_id=bundle_id,
        amount_kopeks=amount_kopeks,
        environment=environment,
        status='verified',
        is_paid=True,
        paid_at=datetime.now(UTC),
        transaction_id_fk=transaction_id_fk,
    )

    db.add(apple_txn)
    await db.flush()
    await db.refresh(apple_txn)

    logger.info(
        'Создана Apple транзакция',
        transaction_id=transaction_id,
        product_id=product_id,
        amount_kopeks=amount_kopeks,
        user_id=user_id,
    )
    return apple_txn


async def get_apple_transaction_by_transaction_id(
    db: AsyncSession, transaction_id: str
) -> AppleTransaction | None:
    result = await db.execute(
        select(AppleTransaction).where(AppleTransaction.transaction_id == transaction_id)
    )
    return result.scalar_one_or_none()


async def mark_apple_transaction_refunded(
    db: AsyncSession, transaction_id: str
) -> AppleTransaction | None:
    """Mark an Apple transaction as refunded. Returns the transaction or None if not found."""
    apple_txn = await get_apple_transaction_by_transaction_id(db, transaction_id)
    if not apple_txn:
        return None

    apple_txn.status = 'refunded'
    apple_txn.refunded_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(apple_txn)

    logger.info(
        'Apple транзакция помечена как возврат',
        transaction_id=transaction_id,
        user_id=apple_txn.user_id,
    )
    return apple_txn
