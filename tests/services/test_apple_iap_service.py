"""Focused tests for Apple IAP domain services."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError

import app.services.apple_iap as apple_iap_module
from app.config import settings
from app.services.apple_iap import AppleFulfillmentResult, AppleIAPFulfillmentService, AppleIAPNotificationService


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


def _enable_apple_iap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cert_path = tmp_path / 'apple-root.cer'
    cert_path.write_bytes(b'dummy-cert')
    monkeypatch.setattr(settings, 'APPLE_IAP_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_KEY_ID', 'TEST_KEY_ID', raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_ISSUER_ID', 'test-issuer-id', raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_BUNDLE_ID', 'com.bitnet.vpnclient', raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_APP_APPLE_ID', 123456789, raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_ENVIRONMENT', 'Sandbox', raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_PRIVATE_KEY', 'private-key', raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_ROOT_CERTS_PATHS', str(cert_path), raising=False)
    monkeypatch.setattr(
        settings,
        'APPLE_IAP_PRODUCTS',
        json.dumps({'com.bitnet.vpnclient.topup.100': 10_000}),
        raising=False,
    )


class _AsyncContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeDB:
    def __init__(self):
        self.commit = AsyncMock()
        self.flush = AsyncMock()
        self.refresh = AsyncMock()

    def begin_nested(self):
        return _AsyncContext()


def _txn_info(transaction_id: str = '2000000123456789') -> dict[str, str]:
    return {
        'transactionId': transaction_id,
        'originalTransactionId': transaction_id,
        'webOrderLineItemId': '2000000099999999',
        'bundleId': 'com.bitnet.vpnclient',
        'productId': 'com.bitnet.vpnclient.topup.100',
        'type': 'Consumable',
        'appAccountToken': 'account-token',
        'environment': 'Sandbox',
    }


@pytest.mark.anyio('asyncio')
async def test_fulfill_verified_transaction_happy_path_credits_balance_after_user_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _enable_apple_iap(monkeypatch, tmp_path)
    db = _FakeDB()
    events: list[str] = []
    user = SimpleNamespace(
        id=1,
        balance_kopeks=1_000,
        has_made_first_topup=False,
        referred_by_id=None,
        subscription=None,
        get_primary_promo_group=lambda: None,
    )
    apple_txn = SimpleNamespace(
        transaction_id='2000000123456789',
        transaction_id_fk=None,
        status='verified',
        credited_at=None,
        updated_at=None,
    )
    transaction = SimpleNamespace(id=42)

    async def lock_user(_db, _user_ref):
        events.append('lock_user')
        return user

    async def create_apple_transaction(**kwargs):
        events.append('create_apple_transaction')
        return apple_txn

    async def create_transaction(**kwargs):
        events.append('create_transaction')
        assert kwargs['commit'] is False
        return transaction

    service = AppleIAPFulfillmentService()
    side_effects = AsyncMock()
    service._emit_credit_side_effects = side_effects  # type: ignore[method-assign]
    monkeypatch.setattr(apple_iap_module, 'get_apple_transaction_by_transaction_id', AsyncMock(return_value=None))
    monkeypatch.setattr(
        apple_iap_module, 'get_apple_transaction_by_web_order_line_item_id', AsyncMock(return_value=None)
    )
    monkeypatch.setattr(apple_iap_module, 'lock_user_for_update', lock_user)
    monkeypatch.setattr(apple_iap_module, 'create_apple_transaction', create_apple_transaction)
    monkeypatch.setattr(apple_iap_module, 'create_transaction', create_transaction)

    result = await service.fulfill_verified_transaction(
        db,
        user_id=1,
        product_id='com.bitnet.vpnclient.topup.100',
        txn_info=_txn_info(),
        expected_app_account_token='account-token',
    )

    assert result == AppleFulfillmentResult(True, 'credited', apple_txn, transaction)
    assert events == ['lock_user', 'create_apple_transaction', 'create_transaction']
    assert apple_txn.transaction_id_fk == 42
    assert apple_txn.status == 'credited'
    assert user.balance_kopeks == 11_000
    db.commit.assert_awaited_once()
    side_effects.assert_awaited_once()


@pytest.mark.anyio('asyncio')
async def test_fulfill_verified_transaction_insert_race_returns_existing_without_double_credit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _enable_apple_iap(monkeypatch, tmp_path)
    db = _FakeDB()
    user = SimpleNamespace(
        id=1,
        balance_kopeks=1_000,
        has_made_first_topup=True,
        referred_by_id=None,
        subscription=None,
        get_primary_promo_group=lambda: None,
    )
    existing = SimpleNamespace(user_id=1, status='credited', transaction_id='2000000123456789')
    create_transaction = AsyncMock()

    monkeypatch.setattr(
        apple_iap_module,
        'get_apple_transaction_by_transaction_id',
        AsyncMock(side_effect=[None, existing]),
    )
    monkeypatch.setattr(
        apple_iap_module, 'get_apple_transaction_by_web_order_line_item_id', AsyncMock(return_value=None)
    )
    monkeypatch.setattr(apple_iap_module, 'lock_user_for_update', AsyncMock(return_value=user))
    monkeypatch.setattr(
        apple_iap_module,
        'create_apple_transaction',
        AsyncMock(side_effect=IntegrityError('insert', {}, Exception('duplicate'))),
    )
    monkeypatch.setattr(apple_iap_module, 'create_transaction', create_transaction)

    result = await AppleIAPFulfillmentService().fulfill_verified_transaction(
        db,
        user_id=1,
        product_id='com.bitnet.vpnclient.topup.100',
        txn_info=_txn_info(),
        expected_app_account_token='account-token',
    )

    assert result.success is True
    assert result.reason == 'already_processed'
    assert result.apple_transaction is existing
    assert user.balance_kopeks == 1_000
    create_transaction.assert_not_awaited()
    db.commit.assert_awaited_once()


@pytest.mark.anyio('asyncio')
async def test_one_time_charge_dispatch_fulfills_account_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDB()
    account = SimpleNamespace(user_id=123, account_token_uuid='account-token')
    fulfillment = SimpleNamespace(
        fulfill_verified_transaction=AsyncMock(return_value=AppleFulfillmentResult(True, 'credited'))
    )
    monkeypatch.setattr(apple_iap_module, 'get_apple_iap_account_by_token', AsyncMock(return_value=account))

    reason = await AppleIAPNotificationService(fulfillment_service=fulfillment)._handle_one_time_charge(db, _txn_info())

    assert reason == 'credited'
    fulfillment.fulfill_verified_transaction.assert_awaited_once_with(
        db,
        user_id=123,
        product_id='com.bitnet.vpnclient.topup.100',
        txn_info=_txn_info(),
        expected_app_account_token='account-token',
    )


@pytest.mark.anyio('asyncio')
async def test_refund_success_debits_balance_and_marks_transaction_refunded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _enable_apple_iap(monkeypatch, tmp_path)
    db = _FakeDB()
    user = SimpleNamespace(id=1, balance_kopeks=20_000)
    apple_txn = SimpleNamespace(
        transaction_id='2000000123456789',
        original_transaction_id='2000000123456789',
        status='credited',
        environment='Sandbox',
        user_id=1,
        amount_kopeks=10_000,
        product_id='com.bitnet.vpnclient.topup.100',
        bundle_id='com.bitnet.vpnclient',
        app_account_token='account-token',
    )
    subtract_balance = AsyncMock()
    mark_refunded = AsyncMock()
    monkeypatch.setattr(
        apple_iap_module, 'get_apple_transaction_by_transaction_id_for_update', AsyncMock(return_value=apple_txn)
    )
    monkeypatch.setattr(apple_iap_module, 'lock_user_for_pricing', AsyncMock(return_value=user))
    monkeypatch.setattr('app.database.crud.user.subtract_user_balance', subtract_balance)
    monkeypatch.setattr(apple_iap_module, 'mark_apple_transaction_refunded', mark_refunded)

    reason = await AppleIAPNotificationService()._handle_refund(db, _txn_info())

    assert reason == 'refunded'
    subtract_balance.assert_awaited_once()
    assert subtract_balance.await_args.kwargs['amount_kopeks'] == 10_000
    assert subtract_balance.await_args.kwargs['commit'] is False
    mark_refunded.assert_awaited_once_with(db, '2000000123456789')


@pytest.mark.anyio('asyncio')
async def test_consumption_request_requires_recorded_user_consent() -> None:
    reason = await AppleIAPNotificationService()._handle_consumption_request(_txn_info())

    assert reason == 'consent_missing'


@pytest.mark.anyio('asyncio')
async def test_notification_payload_hash_insert_race_is_treated_as_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _enable_apple_iap(monkeypatch, tmp_path)
    db = _FakeDB()
    existing_payload = SimpleNamespace(notification_uuid='existing-notification-uuid', status='processed')

    class FakeAppleService:
        def verify_notification(self, signed_payload: str):
            return {
                'notificationUUID': 'new-notification-uuid',
                'notificationType': 'TEST',
                'data': {'environment': 'Sandbox'},
            }

    monkeypatch.setattr(apple_iap_module, 'AsyncSessionLocal', lambda: _AsyncContextWithValue(db))
    monkeypatch.setattr(apple_iap_module, 'get_apple_notification_by_uuid', AsyncMock(return_value=None))
    monkeypatch.setattr(
        apple_iap_module,
        'get_apple_notification_by_payload_hash',
        AsyncMock(side_effect=[None, existing_payload]),
    )
    monkeypatch.setattr(
        apple_iap_module,
        'create_apple_notification',
        AsyncMock(side_effect=IntegrityError('insert', {}, Exception('duplicate'))),
    )

    ok, reason = await AppleIAPNotificationService(FakeAppleService()).process_signed_payload(
        'signed.payload',
        b'{"signedPayload":"signed.payload"}',
    )

    assert ok is True
    assert reason == 'payload_replay'


class _AsyncContextWithValue:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False
