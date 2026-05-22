from datetime import UTC, datetime
from uuid import uuid4

import structlog
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AppleIAPAbuseEvent, AppleIAPAccount, AppleNotification, AppleTransaction


logger = structlog.get_logger(__name__)


async def get_or_create_apple_iap_account(db: AsyncSession, user_id: int) -> AppleIAPAccount:
    """Return a stable StoreKit appAccountToken UUID for a user."""
    result = await db.execute(
        select(AppleIAPAccount)
        .where(AppleIAPAccount.user_id == user_id, AppleIAPAccount.disabled_at.is_(None))
        .with_for_update()
    )
    account = result.scalar_one_or_none()
    if account:
        return account

    try:
        async with db.begin_nested():
            account = AppleIAPAccount(user_id=user_id, account_token_uuid=str(uuid4()))
            db.add(account)
            await db.flush()
    except IntegrityError:
        result = await db.execute(
            select(AppleIAPAccount)
            .where(AppleIAPAccount.user_id == user_id, AppleIAPAccount.disabled_at.is_(None))
            .with_for_update()
        )
        account = result.scalar_one()
    await db.refresh(account)
    return account


async def get_apple_iap_account_by_token(db: AsyncSession, account_token_uuid: str) -> AppleIAPAccount | None:
    result = await db.execute(
        select(AppleIAPAccount).where(
            AppleIAPAccount.account_token_uuid == account_token_uuid,
            AppleIAPAccount.disabled_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


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
    app_account_token: str | None = None,
    web_order_line_item_id: str | None = None,
    storefront: str | None = None,
    currency: str | None = None,
    price_micros: int | None = None,
    purchase_date: datetime | None = None,
    revocation_date: datetime | None = None,
    revocation_reason: str | None = None,
    signed_transaction_hash: str | None = None,
    metadata_json: dict | None = None,
    status: str = 'verified',
    is_paid: bool = True,
    credited_at: datetime | None = None,
) -> AppleTransaction:
    now = datetime.now(UTC)
    apple_txn = AppleTransaction(
        user_id=user_id,
        transaction_id=transaction_id,
        original_transaction_id=original_transaction_id,
        product_id=product_id,
        bundle_id=bundle_id,
        amount_kopeks=amount_kopeks,
        environment=environment,
        app_account_token=app_account_token,
        web_order_line_item_id=web_order_line_item_id,
        storefront=storefront,
        currency=currency,
        price_micros=price_micros,
        purchase_date=purchase_date,
        revocation_date=revocation_date,
        revocation_reason=revocation_reason,
        status=status,
        is_paid=is_paid,
        paid_at=now if is_paid else None,
        credited_at=credited_at,
        transaction_id_fk=transaction_id_fk,
        signed_transaction_hash=signed_transaction_hash,
        metadata_json=metadata_json,
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


async def get_apple_transaction_by_transaction_id(db: AsyncSession, transaction_id: str) -> AppleTransaction | None:
    result = await db.execute(select(AppleTransaction).where(AppleTransaction.transaction_id == transaction_id))
    return result.scalar_one_or_none()


async def get_apple_transaction_by_web_order_line_item_id(
    db: AsyncSession, web_order_line_item_id: str
) -> AppleTransaction | None:
    result = await db.execute(
        select(AppleTransaction).where(AppleTransaction.web_order_line_item_id == web_order_line_item_id)
    )
    return result.scalar_one_or_none()


async def find_apple_transactions_for_support(db: AsyncSession, query: str, limit: int = 20) -> list[AppleTransaction]:
    filters = [
        AppleTransaction.transaction_id == query,
        AppleTransaction.original_transaction_id == query,
        AppleTransaction.web_order_line_item_id == query,
        AppleTransaction.signed_transaction_hash == query,
    ]
    if query.isdigit():
        filters.append(AppleTransaction.user_id == int(query))
    result = await db.execute(
        select(AppleTransaction).where(or_(*filters)).order_by(AppleTransaction.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def get_recent_apple_transactions(db: AsyncSession, limit: int = 100) -> list[AppleTransaction]:
    result = await db.execute(select(AppleTransaction).order_by(AppleTransaction.created_at.desc()).limit(limit))
    return list(result.scalars().all())


async def get_unprocessed_apple_notifications(db: AsyncSession, limit: int = 100) -> list[AppleNotification]:
    result = await db.execute(
        select(AppleNotification)
        .where(AppleNotification.status.in_(['received', 'failed']))
        .order_by(AppleNotification.received_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_apple_transaction_by_transaction_id_for_update(
    db: AsyncSession, transaction_id: str
) -> AppleTransaction | None:
    """Get apple transaction with FOR UPDATE lock for safe concurrent access."""
    result = await db.execute(
        select(AppleTransaction).where(AppleTransaction.transaction_id == transaction_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def mark_apple_transaction_refunded(db: AsyncSession, transaction_id: str) -> AppleTransaction | None:
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


async def create_apple_notification(
    db: AsyncSession,
    notification_uuid: str,
    notification_type: str,
    payload_hash: str,
    *,
    subtype: str | None = None,
    environment: str | None = None,
    transaction_id: str | None = None,
    original_transaction_id: str | None = None,
    status: str = 'received',
    metadata_json: dict | None = None,
) -> AppleNotification:
    notification = AppleNotification(
        notification_uuid=notification_uuid,
        notification_type=notification_type,
        subtype=subtype,
        environment=environment,
        transaction_id=transaction_id,
        original_transaction_id=original_transaction_id,
        status=status,
        payload_hash=payload_hash,
        metadata_json=metadata_json,
    )
    db.add(notification)
    await db.flush()
    await db.refresh(notification)
    return notification


async def get_apple_notification_by_uuid(db: AsyncSession, notification_uuid: str) -> AppleNotification | None:
    result = await db.execute(
        select(AppleNotification).where(AppleNotification.notification_uuid == notification_uuid).with_for_update()
    )
    return result.scalar_one_or_none()


async def get_apple_notification_by_payload_hash(db: AsyncSession, payload_hash: str) -> AppleNotification | None:
    result = await db.execute(
        select(AppleNotification).where(AppleNotification.payload_hash == payload_hash).with_for_update()
    )
    return result.scalar_one_or_none()


async def mark_apple_notification_processed(
    db: AsyncSession,
    notification: AppleNotification,
    *,
    status: str = 'processed',
    error: str | None = None,
) -> AppleNotification:
    notification.status = status
    notification.error = error
    notification.processed_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(notification)
    return notification


async def create_apple_abuse_event(
    db: AsyncSession,
    event_type: str,
    *,
    user_id: int | None = None,
    severity: str = 'warning',
    transaction_id: str | None = None,
    product_id: str | None = None,
    ip_address: str | None = None,
    details_json: dict | None = None,
) -> AppleIAPAbuseEvent:
    event = AppleIAPAbuseEvent(
        user_id=user_id,
        event_type=event_type,
        severity=severity,
        transaction_id=transaction_id,
        product_id=product_id,
        ip_address=ip_address,
        details_json=details_json,
    )
    db.add(event)
    await db.flush()
    await db.refresh(event)
    logger.warning(
        'Apple IAP abuse event recorded',
        event_type=event_type,
        user_id=user_id,
        severity=severity,
        transaction_id=transaction_id,
    )
    return event
