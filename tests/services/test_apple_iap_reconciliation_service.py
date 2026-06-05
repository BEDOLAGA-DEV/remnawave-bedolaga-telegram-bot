"""Tests for Apple IAP reconciliation and support helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.apple_iap_reconciliation_service as reconciliation_module
from app.services.apple_iap_reconciliation_service import AppleIAPReconciliationService


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


class _FakeDB:
    def __init__(self):
        self.commit = AsyncMock()


@pytest.mark.anyio('asyncio')
async def test_lookup_delegates_to_support_query(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDB()
    rows = [SimpleNamespace(transaction_id='2000000123456789')]
    lookup = AsyncMock(return_value=rows)
    monkeypatch.setattr(reconciliation_module, 'find_apple_transactions_for_support', lookup)

    result = await AppleIAPReconciliationService().lookup(db, '2000000123456789', limit=5)

    assert result == rows
    lookup.assert_awaited_once_with(db, '2000000123456789', limit=5)


@pytest.mark.anyio('asyncio')
async def test_reconcile_recent_transactions_flags_drift_and_counts_backlog(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDB()
    sandbox_txn = SimpleNamespace(
        transaction_id='sandbox-txn',
        environment='Sandbox',
        user_id=1,
        product_id='com.bitnet.vpnclient.topup.100',
        status='credited',
        app_account_token='sandbox-token',
    )
    fetch_failed_txn = SimpleNamespace(
        transaction_id='missing-txn',
        environment='Production',
        user_id=2,
        product_id='com.bitnet.vpnclient.topup.100',
        status='credited',
        app_account_token='token-2',
    )
    drift_txn = SimpleNamespace(
        transaction_id='drift-txn',
        environment='Production',
        user_id=3,
        product_id='com.bitnet.vpnclient.topup.100',
        status='credited',
        app_account_token='token-3',
    )
    notifications = [SimpleNamespace(notification_uuid='n1'), SimpleNamespace(notification_uuid='n2')]
    abuse_event = AsyncMock()

    class FakeAppleService:
        def __init__(self):
            self.verify_transaction = AsyncMock(
                side_effect=[
                    None,
                    {
                        'productId': 'com.bitnet.vpnclient.topup.300',
                        'revocationDate': 1_700_000_000_000,
                        'appAccountToken': 'other-token',
                    },
                ]
            )

    apple_service = FakeAppleService()
    monkeypatch.setattr(
        reconciliation_module,
        'get_recent_apple_transactions',
        AsyncMock(return_value=[sandbox_txn, fetch_failed_txn, drift_txn]),
    )
    monkeypatch.setattr(
        reconciliation_module, 'get_unprocessed_apple_notifications', AsyncMock(return_value=notifications)
    )
    monkeypatch.setattr(reconciliation_module, 'create_apple_abuse_event', abuse_event)

    result = await AppleIAPReconciliationService(apple_service).reconcile_recent_transactions(db, limit=10)

    assert result.checked == 3
    assert result.drift_count == 2
    assert result.notification_backlog == 2
    assert apple_service.verify_transaction.await_count == 2
    apple_service.verify_transaction.assert_any_await('missing-txn', 'Production')
    apple_service.verify_transaction.assert_any_await('drift-txn', 'Production')
    assert abuse_event.await_count == 2
    assert abuse_event.await_args_list[0].args[1] == 'reconciliation_fetch_failed'
    assert abuse_event.await_args_list[1].args[1] == 'reconciliation_drift'
    db.commit.assert_awaited_once()
