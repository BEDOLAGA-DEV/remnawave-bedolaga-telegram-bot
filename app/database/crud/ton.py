from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import TonPayment


logger = structlog.get_logger(__name__)


async def create_ton_payment(
    db: AsyncSession,
    *,
    user_id: int,
    memo: str,
    amount_kopeks: int,
    amount_nano: int,
    expires_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> TonPayment:
    payment = TonPayment(
        user_id=user_id,
        memo=memo,
        amount_kopeks=amount_kopeks,
        amount_nano=amount_nano,
        status='pending',
        expires_at=expires_at,
        metadata_json=metadata or {},
    )

    db.add(payment)
    await db.commit()
    await db.refresh(payment)

    logger.info(
        'Создан TON платёж: memo= amount_kopeks= для пользователя',
        memo=memo,
        amount_kopeks=amount_kopeks,
        amount_nano=amount_nano,
        user_id=user_id,
    )

    return payment


async def get_ton_payment_by_memo(
    db: AsyncSession,
    memo: str,
) -> TonPayment | None:
    result = await db.execute(
        select(TonPayment).options(selectinload(TonPayment.user)).where(TonPayment.memo == memo)
    )
    return result.scalar_one_or_none()


async def get_ton_payment_by_id(
    db: AsyncSession,
    payment_id: int,
) -> TonPayment | None:
    result = await db.execute(
        select(TonPayment).options(selectinload(TonPayment.user)).where(TonPayment.id == payment_id)
    )
    return result.scalar_one_or_none()


async def update_ton_payment(
    db: AsyncSession,
    payment: TonPayment,
    *,
    status: str | None = None,
    paid_at: datetime | None = None,
    ton_hash: str | None = None,
    metadata: dict[str, Any] | None = None,
    callback_payload: dict[str, Any] | None = None,
) -> TonPayment:
    if status is not None:
        payment.status = status
    if paid_at is not None:
        payment.paid_at = paid_at
    if ton_hash is not None:
        payment.ton_hash = ton_hash
    if metadata is not None:
        existing = dict(payment.metadata_json or {})
        existing.update(metadata)
        payment.metadata_json = existing
    if callback_payload is not None:
        payment.callback_payload = callback_payload

    payment.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(payment)

    logger.info(
        'Обновлён TON платёж: memo= статус=',
        memo=payment.memo,
        status=payment.status,
    )

    return payment


async def link_ton_payment_to_transaction(
    db: AsyncSession,
    payment: TonPayment,
    transaction_id: int,
) -> TonPayment:
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(payment)

    logger.info('TON платёж связан с транзакцией', memo=payment.memo, transaction_id=transaction_id)

    return payment
