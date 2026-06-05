"""Reconciliation and support helpers for Apple IAP."""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.apple_iap import (
    create_apple_abuse_event,
    find_apple_transactions_for_support,
    get_recent_apple_transactions,
    get_unprocessed_apple_notifications,
)
from app.database.models import AppleTransaction
from app.external.apple_iap import AppleIAPService


logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class AppleReconciliationResult:
    checked: int
    drift_count: int
    notification_backlog: int


class AppleIAPReconciliationService:
    def __init__(self, apple_service: AppleIAPService | None = None):
        self.apple_service = apple_service or AppleIAPService()

    async def lookup(self, db: AsyncSession, query: str, limit: int = 20) -> list[AppleTransaction]:
        """Support lookup by user id, transaction id, original transaction id, or payload hash."""
        return await find_apple_transactions_for_support(db, query, limit=limit)

    async def reconcile_recent_transactions(self, db: AsyncSession, limit: int = 100) -> AppleReconciliationResult:
        """Re-fetch recent transactions from Apple and flag status drift for review."""
        transactions = await get_recent_apple_transactions(db, limit=limit)
        drift_count = 0

        for apple_txn in transactions:
            if apple_txn.environment == 'Sandbox':
                continue
            fetched = await self.apple_service.verify_transaction(apple_txn.transaction_id, apple_txn.environment)
            if not fetched:
                drift_count += 1
                await create_apple_abuse_event(
                    db,
                    'reconciliation_fetch_failed',
                    user_id=apple_txn.user_id,
                    transaction_id=apple_txn.transaction_id,
                    product_id=apple_txn.product_id,
                    details_json={'stored_status': apple_txn.status},
                )
                continue

            drift: dict[str, object] = {}
            if fetched.get('productId') != apple_txn.product_id:
                drift['product_id'] = {'stored': apple_txn.product_id, 'apple': fetched.get('productId')}
            if fetched.get('revocationDate') and apple_txn.status != 'refunded':
                drift['revocation'] = fetched.get('revocationDate')
            if fetched.get('appAccountToken') and fetched.get('appAccountToken') != apple_txn.app_account_token:
                drift['app_account_token'] = 'mismatch'

            if drift:
                drift_count += 1
                await create_apple_abuse_event(
                    db,
                    'reconciliation_drift',
                    user_id=apple_txn.user_id,
                    transaction_id=apple_txn.transaction_id,
                    product_id=apple_txn.product_id,
                    details_json=drift,
                )

        notifications = await get_unprocessed_apple_notifications(db, limit=limit)
        await db.commit()
        logger.info('Apple IAP reconciliation complete', checked=len(transactions), drift_count=drift_count)
        return AppleReconciliationResult(
            checked=len(transactions),
            drift_count=drift_count,
            notification_backlog=len(notifications),
        )


apple_iap_reconciliation_service = AppleIAPReconciliationService()
