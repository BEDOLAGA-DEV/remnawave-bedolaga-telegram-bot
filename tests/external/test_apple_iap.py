"""Tests for Apple In-App Purchase library-backed integration."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.config as config_module
import app.services.apple_iap as apple_iap_module
from app.cabinet.schemas.apple_iap import AppleAccountTokenResponse, ApplePurchaseRequest
from app.config import settings
from app.external.apple_iap import AppleIAPService, parse_apple_timestamp
from app.services.apple_iap import AppleFulfillmentResult, AppleIAPFulfillmentService, AppleIAPNotificationService


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


def _enable_apple_iap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path | None = None) -> Path:
    cert_path = (tmp_path or Path('/tmp')).joinpath('apple-root.cer')  # noqa: S108
    cert_path.write_bytes(b'dummy-cert')
    monkeypatch.setattr(settings, 'APPLE_IAP_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_KEY_ID', 'TEST_KEY_ID', raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_ISSUER_ID', 'test-issuer-id', raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_BUNDLE_ID', 'com.bitnet.vpnclient', raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_APP_APPLE_ID', 123456789, raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_ENVIRONMENT', 'Sandbox', raising=False)
    monkeypatch.setattr(
        settings,
        'APPLE_IAP_PRIVATE_KEY',
        '-----BEGIN PRIVATE KEY-----\\nkey\\n-----END PRIVATE KEY-----',
        raising=False,
    )
    monkeypatch.setattr(settings, 'APPLE_IAP_PRIVATE_KEY_PATH', None, raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_ROOT_CERTS_PATHS', str(cert_path), raising=False)
    monkeypatch.setattr(
        settings,
        'APPLE_IAP_PRODUCTS',
        json.dumps(
            {
                'com.bitnet.vpnclient.topup.100': 10_000,
                'com.bitnet.vpnclient.topup.300': 30_000,
            }
        ),
        raising=False,
    )
    return cert_path


class TestAppleDependency:
    def test_official_library_imports(self) -> None:
        from appstoreserverlibrary.api_client import AsyncAppStoreServerAPIClient
        from appstoreserverlibrary.models.Environment import Environment
        from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier

        assert AsyncAppStoreServerAPIClient.__name__ == 'AsyncAppStoreServerAPIClient'
        assert Environment.SANDBOX.name == 'SANDBOX'
        assert SignedDataVerifier.__name__ == 'SignedDataVerifier'


class TestSettings:
    def test_enabled_with_required_params(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _enable_apple_iap(monkeypatch, tmp_path)
        assert settings.is_apple_iap_enabled() is True

    def test_production_requires_app_apple_id(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _enable_apple_iap(monkeypatch, tmp_path)
        monkeypatch.setattr(settings, 'APPLE_IAP_ENVIRONMENT', 'Production', raising=False)
        monkeypatch.setattr(settings, 'APPLE_IAP_APP_APPLE_ID', None, raising=False)
        assert settings.is_apple_iap_enabled() is False

    def test_missing_root_cert_disables(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _enable_apple_iap(monkeypatch, tmp_path)
        monkeypatch.setattr(settings, 'APPLE_IAP_ROOT_CERTS_PATHS', '', raising=False)
        assert settings.is_apple_iap_enabled() is False

    def test_blank_key_metadata_disables(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _enable_apple_iap(monkeypatch, tmp_path)
        monkeypatch.setattr(settings, 'APPLE_IAP_KEY_ID', ' ', raising=False)
        assert settings.is_apple_iap_enabled() is False

    def test_blank_private_key_disables(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _enable_apple_iap(monkeypatch, tmp_path)
        monkeypatch.setattr(settings, 'APPLE_IAP_PRIVATE_KEY', '', raising=False)
        monkeypatch.setattr(settings, 'APPLE_IAP_PRIVATE_KEY_PATH', '', raising=False)
        assert settings.is_apple_iap_enabled() is False

    def test_private_key_file_read_error_is_logged(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        missing_key_path = tmp_path / 'missing-auth-key.p8'
        error_log = MagicMock()
        monkeypatch.setattr(settings, 'APPLE_IAP_PRIVATE_KEY', '', raising=False)
        monkeypatch.setattr(settings, 'APPLE_IAP_PRIVATE_KEY_PATH', str(missing_key_path), raising=False)
        monkeypatch.setattr(config_module.logger, 'error', error_log)

        assert settings.get_apple_iap_private_key() is None

        error_log.assert_called_once()
        assert error_log.call_args.args[0] == 'Failed to load Apple IAP private key file'
        assert error_log.call_args.kwargs['path'] == str(missing_key_path)
        assert error_log.call_args.kwargs['exc_info'] is True

    def test_product_mapping_normalizes_positive_ints(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            settings,
            'APPLE_IAP_PRODUCTS',
            json.dumps({'valid': '100', 'zero': 0, 'bad': 'nope'}),
            raising=False,
        )
        assert settings.get_apple_iap_products() == {'valid': 100}

    def test_environment_defaults_to_production(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, 'APPLE_IAP_ENVIRONMENT', 'invalid', raising=False)
        assert settings.get_apple_iap_environment() == 'Production'


class TestTransactionValidation:
    def test_valid_transaction(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _enable_apple_iap(monkeypatch, tmp_path)
        service = AppleIAPService()
        txn_info = {
            'bundleId': 'com.bitnet.vpnclient',
            'productId': 'com.bitnet.vpnclient.topup.100',
            'type': 'Consumable',
        }
        assert service.validate_transaction_info(txn_info, 'com.bitnet.vpnclient.topup.100') is None

    def test_rejects_wrong_bundle(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _enable_apple_iap(monkeypatch, tmp_path)
        result = AppleIAPService().validate_transaction_info(
            {'bundleId': 'other', 'productId': 'com.bitnet.vpnclient.topup.100', 'type': 'Consumable'},
            'com.bitnet.vpnclient.topup.100',
        )
        assert result and 'Bundle ID' in result

    def test_rejects_revoked_transaction(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _enable_apple_iap(monkeypatch, tmp_path)
        result = AppleIAPService().validate_transaction_info(
            {
                'bundleId': 'com.bitnet.vpnclient',
                'productId': 'com.bitnet.vpnclient.topup.100',
                'type': 'Consumable',
                'revocationDate': 1700000000000,
            },
            'com.bitnet.vpnclient.topup.100',
        )
        assert result and 'revoked' in result.lower()


class TestAdapter:
    @pytest.mark.anyio('asyncio')
    async def test_verify_transaction_uses_client_and_verifier(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _enable_apple_iap(monkeypatch, tmp_path)
        service = AppleIAPService()

        class FakeClient:
            async def get_transaction_info(self, transaction_id: str):
                assert transaction_id == '2000000123456789'
                return MagicMock(signedTransactionInfo='signed.txn.info')

            async def async_close(self):
                return None

        class FakeVerifier:
            def verify_and_decode_signed_transaction(self, signed_transaction_info: str):
                assert signed_transaction_info == 'signed.txn.info'
                return {
                    'bundleId': 'com.bitnet.vpnclient',
                    'productId': 'com.bitnet.vpnclient.topup.100',
                    'type': 'Consumable',
                    'transactionId': '2000000123456789',
                    'environment': 'Sandbox',
                }

        monkeypatch.setattr(service, '_client', lambda environment: FakeClient())
        monkeypatch.setattr(service, '_verifier', lambda environment=None: FakeVerifier())

        result = await service.verify_transaction('2000000123456789', 'Sandbox')

        assert result is not None
        assert result['transactionId'] == '2000000123456789'
        assert result['signedTransactionInfoHash']

    def test_verify_notification_uses_signed_data_verifier(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _enable_apple_iap(monkeypatch, tmp_path)
        service = AppleIAPService()

        class FakeVerifier:
            def verify_and_decode_notification(self, signed_payload: str):
                assert signed_payload == 'signed.payload'
                return {'notificationUUID': 'uuid', 'notificationType': 'TEST', 'data': {'environment': 'Sandbox'}}

        monkeypatch.setattr(service, '_verifier', lambda environment=None: FakeVerifier())

        result = service.verify_notification('signed.payload', 'Sandbox')

        assert result is not None
        assert result['notificationUUID'] == 'uuid'
        assert result['signedPayloadHash']


class TestSchemas:
    def test_valid_purchase_request(self) -> None:
        req = ApplePurchaseRequest(
            product_id='com.bitnet.vpnclient.topup.100',
            transaction_id='2000000123456789',
        )
        assert req.transaction_id == '2000000123456789'

    def test_rejects_non_numeric_transaction_id(self) -> None:
        with pytest.raises(Exception, match='digits'):
            ApplePurchaseRequest(product_id='com.bitnet.vpnclient.topup.100', transaction_id='abc')

    def test_rejects_empty_transaction_id(self) -> None:
        with pytest.raises(ValidationError):
            ApplePurchaseRequest(product_id='com.bitnet.vpnclient.topup.100', transaction_id='')

    def test_account_token_response(self) -> None:
        response = AppleAccountTokenResponse(app_account_token='123e4567-e89b-12d3-a456-426614174000')
        assert response.app_account_token.endswith('4000')


class TestTimestampParsing:
    def test_parse_apple_millis(self) -> None:
        parsed = parse_apple_timestamp(1_700_000_000_000)
        assert parsed is not None
        assert parsed.tzinfo is not None

    def test_parse_invalid_returns_none(self) -> None:
        assert parse_apple_timestamp('not-a-date') is None


class _AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeDB:
    def __init__(self):
        self.commit = AsyncMock()
        self.flush = AsyncMock()

    def begin_nested(self):
        return _AsyncContext()


class TestCabinetAppleIAPRoutes:
    @pytest.mark.anyio('asyncio')
    async def test_purchase_rate_limit_decodes_redis_bytes_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.cabinet import apple_iap as apple_iap_routes

        class FakeRedis:
            async def get(self, key: str) -> bytes:
                assert key == 'apple_iap:purchase_fail:user:1'
                return b'3'

            async def incr(self, key: str) -> int:  # pragma: no cover - failure counter blocks before incr
                raise AssertionError(f'incr should not be called for {key}')

        monkeypatch.setattr(settings, 'APPLE_IAP_PURCHASE_FAILURE_LIMIT_PER_HOUR', 3, raising=False)

        assert await apple_iap_routes._check_purchase_rate_limit(FakeRedis(), user_id=1, ip_address=None) is False

    @pytest.mark.anyio('asyncio')
    async def test_purchase_rate_limit_blocks_invalid_redis_counter_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.cabinet import apple_iap as apple_iap_routes

        class FakeRedis:
            async def get(self, key: str) -> bytes:
                assert key == 'apple_iap:purchase_fail:user:1'
                return b'not-an-int'

            async def incr(self, key: str) -> int:  # pragma: no cover - invalid failure counter fails open first
                raise AssertionError(f'incr should not be called for {key}')

        monkeypatch.setattr(settings, 'APPLE_IAP_RATE_LIMIT_FAIL_OPEN', False, raising=False)

        assert await apple_iap_routes._check_purchase_rate_limit(FakeRedis(), user_id=1, ip_address=None) is False

    @pytest.mark.anyio('asyncio')
    async def test_purchase_rate_limit_can_fail_open_by_setting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.cabinet import apple_iap as apple_iap_routes

        class FakeRedis:
            async def get(self, key: str) -> bytes:
                assert key == 'apple_iap:purchase_fail:user:1'
                return b'not-an-int'

        monkeypatch.setattr(settings, 'APPLE_IAP_RATE_LIMIT_FAIL_OPEN', True, raising=False)

        assert await apple_iap_routes._check_purchase_rate_limit(FakeRedis(), user_id=1, ip_address=None) is True

    @pytest.mark.anyio('asyncio')
    async def test_purchase_rate_limit_sets_ttl_atomically_for_new_counter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.cabinet import apple_iap as apple_iap_routes

        class FakeRedis:
            def __init__(self):
                self.commands: list[tuple] = []

            async def get(self, key: str) -> None:
                assert key == 'apple_iap:purchase_fail:user:1'

            async def set(self, key: str, value: int, *, ex: int, nx: bool) -> bool:
                self.commands.append(('set', key, value, ex, nx))
                return True

            async def incr(self, key: str) -> int:  # pragma: no cover - SET NX creates first counter
                raise AssertionError(f'incr should not be called for new counter {key}')

            async def expire(self, key: str, ttl: int) -> None:  # pragma: no cover - TTL must be set with SET
                raise AssertionError(f'expire should not be called for {key} with ttl={ttl}')

        client = FakeRedis()
        monkeypatch.setattr(settings, 'APPLE_IAP_PURCHASE_RATE_LIMIT_PER_MINUTE', 10, raising=False)

        assert await apple_iap_routes._check_purchase_rate_limit(client, user_id=1, ip_address=None) is True
        assert client.commands == [('set', 'apple_iap:purchase:user:1', 1, 60, True)]

    @pytest.mark.anyio('asyncio')
    async def test_purchase_rate_limit_increments_existing_counter_without_expire(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.cabinet import apple_iap as apple_iap_routes

        class FakeRedis:
            def __init__(self):
                self.commands: list[tuple] = []

            async def get(self, key: str) -> None:
                assert key == 'apple_iap:purchase_fail:user:1'

            async def set(self, key: str, value: int, *, ex: int, nx: bool) -> bool:
                self.commands.append(('set', key, value, ex, nx))
                return False

            async def incr(self, key: str) -> bytes:
                self.commands.append(('incr', key))
                return b'2'

            async def expire(self, key: str, ttl: int) -> None:  # pragma: no cover - existing key already has TTL
                raise AssertionError(f'expire should not be called for {key} with ttl={ttl}')

        client = FakeRedis()
        monkeypatch.setattr(settings, 'APPLE_IAP_PURCHASE_RATE_LIMIT_PER_MINUTE', 10, raising=False)

        assert await apple_iap_routes._check_purchase_rate_limit(client, user_id=1, ip_address=None) is True
        assert client.commands == [
            ('set', 'apple_iap:purchase:user:1', 1, 60, True),
            ('incr', 'apple_iap:purchase:user:1'),
        ]

    @pytest.mark.anyio('asyncio')
    async def test_apple_iap_redis_client_uses_lifespan_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fastapi import FastAPI

        from app.cabinet import apple_iap as apple_iap_routes

        class FakeRedis:
            def __init__(self):
                self.closed = False

            async def aclose(self) -> None:
                self.closed = True

        clients: list[FakeRedis] = []

        def from_url(url: str) -> FakeRedis:
            assert url == settings.REDIS_URL
            client = FakeRedis()
            clients.append(client)
            return client

        app = FastAPI()
        monkeypatch.setattr(apple_iap_routes.redis, 'from_url', from_url)

        async with apple_iap_routes.apple_iap_lifespan(app):
            assert len(clients) == 1
            assert getattr(app.state, apple_iap_routes.APPLE_IAP_REDIS_STATE_KEY) is clients[0]

        assert clients[0].closed is True
        assert getattr(app.state, apple_iap_routes.APPLE_IAP_REDIS_STATE_KEY) is None

    @pytest.mark.anyio('asyncio')
    async def test_apple_iap_router_lifespan_initializes_app_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fastapi import FastAPI

        from app.cabinet import apple_iap as apple_iap_routes

        class FakeRedis:
            async def aclose(self) -> None:
                return None

        client = FakeRedis()
        app = FastAPI()
        app.include_router(apple_iap_routes.router)
        monkeypatch.setattr(apple_iap_routes.redis, 'from_url', lambda _url: client)

        async with app.router.lifespan_context(app):
            assert getattr(app.state, apple_iap_routes.APPLE_IAP_REDIS_STATE_KEY) is client

    @pytest.mark.anyio('asyncio')
    async def test_account_token_requires_full_apple_iap_configuration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fastapi import HTTPException

        from app.cabinet.apple_iap import apple_iap_account_token

        get_account_token = AsyncMock(return_value='123e4567-e89b-12d3-a456-426614174000')
        monkeypatch.setattr(settings, 'APPLE_IAP_ENABLED', True, raising=False)
        monkeypatch.setattr(settings, 'APPLE_IAP_ROOT_CERTS_PATHS', '', raising=False)

        with pytest.raises(HTTPException) as exc_info:
            await apple_iap_account_token(
                user=SimpleNamespace(id=1),
                db=_FakeDB(),
                fulfillment_service=SimpleNamespace(get_account_token=get_account_token),
            )

        assert exc_info.value.status_code == 400
        assert 'not fully configured' in exc_info.value.detail
        get_account_token.assert_not_awaited()


class TestFulfillmentService:
    @pytest.mark.anyio('asyncio')
    async def test_rejects_sandbox_transaction_when_production_disallows_sandbox(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _enable_apple_iap(monkeypatch, tmp_path)
        monkeypatch.setattr(settings, 'APPLE_IAP_ENVIRONMENT', 'Production', raising=False)
        monkeypatch.setattr(settings, 'APPLE_IAP_ALLOW_SANDBOX_ON_PRODUCTION', False, raising=False)
        abuse_event = AsyncMock()
        monkeypatch.setattr(apple_iap_module, 'create_apple_abuse_event', abuse_event)

        result = await AppleIAPFulfillmentService().fulfill_verified_transaction(
            _FakeDB(),
            user_id=1,
            product_id='com.bitnet.vpnclient.topup.100',
            expected_app_account_token='account-token',
            txn_info={
                'transactionId': '2000000123456789',
                'bundleId': 'com.bitnet.vpnclient',
                'productId': 'com.bitnet.vpnclient.topup.100',
                'type': 'Consumable',
                'appAccountToken': 'account-token',
                'environment': 'Sandbox',
            },
        )

        assert result.success is False
        assert result.reason == 'environment_mismatch'
        abuse_event.assert_awaited_once()

    @pytest.mark.anyio('asyncio')
    async def test_records_sandbox_transaction_when_production_allows_sandbox(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _enable_apple_iap(monkeypatch, tmp_path)
        monkeypatch.setattr(settings, 'APPLE_IAP_ENVIRONMENT', 'Production', raising=False)
        monkeypatch.setattr(settings, 'APPLE_IAP_ALLOW_SANDBOX_ON_PRODUCTION', True, raising=False)
        service = AppleIAPFulfillmentService()
        record_sandbox = AsyncMock(return_value=AppleFulfillmentResult(True, 'sandbox_recorded'))
        monkeypatch.setattr(service, '_record_sandbox_on_production', record_sandbox)

        result = await service.fulfill_verified_transaction(
            _FakeDB(),
            user_id=1,
            product_id='com.bitnet.vpnclient.topup.100',
            expected_app_account_token='account-token',
            txn_info={
                'transactionId': '2000000123456789',
                'bundleId': 'com.bitnet.vpnclient',
                'productId': 'com.bitnet.vpnclient.topup.100',
                'type': 'Consumable',
                'appAccountToken': 'account-token',
                'environment': 'Sandbox',
            },
        )

        assert result.success is True
        assert result.reason == 'sandbox_recorded'
        record_sandbox.assert_awaited_once()

    @pytest.mark.anyio('asyncio')
    async def test_purchase_verification_respects_sandbox_fallback_setting(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _enable_apple_iap(monkeypatch, tmp_path)
        monkeypatch.setattr(settings, 'APPLE_IAP_ENVIRONMENT', 'Production', raising=False)
        monkeypatch.setattr(settings, 'APPLE_IAP_ALLOW_SANDBOX_ON_PRODUCTION', False, raising=False)
        monkeypatch.setattr(apple_iap_module, 'create_apple_abuse_event', AsyncMock())

        class FakeAppleService:
            def __init__(self):
                self.verify_transaction = AsyncMock(return_value=None)

        apple_service = FakeAppleService()
        result = await AppleIAPFulfillmentService(apple_service).verify_and_fulfill_purchase(
            _FakeDB(),
            SimpleNamespace(id=1),
            product_id='com.bitnet.vpnclient.topup.100',
            transaction_id='2000000123456789',
        )

        assert result.success is False
        apple_service.verify_transaction.assert_awaited_once_with(
            '2000000123456789',
            'Production',
            allow_environment_fallback=False,
        )

    @pytest.mark.anyio('asyncio')
    async def test_web_order_line_item_replay_is_already_processed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _enable_apple_iap(monkeypatch, tmp_path)
        existing = SimpleNamespace(
            user_id=1,
            status='credited',
            transaction_id='2000000123456789',
            web_order_line_item_id='2000000099999999',
        )
        create_transaction = AsyncMock()
        monkeypatch.setattr(apple_iap_module, 'get_apple_transaction_by_transaction_id', AsyncMock(return_value=None))
        monkeypatch.setattr(
            apple_iap_module,
            'get_apple_transaction_by_web_order_line_item_id',
            AsyncMock(return_value=existing),
        )
        monkeypatch.setattr(apple_iap_module, 'create_transaction', create_transaction)
        monkeypatch.setattr(apple_iap_module, 'lock_user_for_update', AsyncMock())

        result = await AppleIAPFulfillmentService().fulfill_verified_transaction(
            _FakeDB(),
            user_id=1,
            product_id='com.bitnet.vpnclient.topup.100',
            expected_app_account_token='account-token',
            txn_info={
                'transactionId': '2000000123456790',
                'originalTransactionId': '2000000123456789',
                'webOrderLineItemId': '2000000099999999',
                'bundleId': 'com.bitnet.vpnclient',
                'productId': 'com.bitnet.vpnclient.topup.100',
                'type': 'Consumable',
                'appAccountToken': 'account-token',
                'environment': 'Sandbox',
            },
        )

        assert result.success is True
        assert result.reason == 'already_processed'
        assert result.apple_transaction is existing
        create_transaction.assert_not_awaited()

    @pytest.mark.anyio('asyncio')
    async def test_successful_credit_locks_user_before_financial_rows(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _enable_apple_iap(monkeypatch, tmp_path)
        db = _FakeDB()
        order: list[str] = []
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
            original_transaction_id=None,
            transaction_id_fk=None,
            status='verified',
            credited_at=None,
            updated_at=None,
        )
        transaction = SimpleNamespace(id=42)

        async def lock_user(_db, _user_ref):
            order.append('lock_user')
            return user

        async def create_apple_txn(**kwargs):
            order.append('create_apple_transaction')
            return apple_txn

        async def create_financial_txn(**kwargs):
            order.append('create_transaction')
            return transaction

        service = AppleIAPFulfillmentService()
        service._emit_credit_side_effects = AsyncMock()  # type: ignore[method-assign]
        monkeypatch.setattr(apple_iap_module, 'get_apple_transaction_by_transaction_id', AsyncMock(return_value=None))
        monkeypatch.setattr(
            apple_iap_module,
            'get_apple_transaction_by_web_order_line_item_id',
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(apple_iap_module, 'lock_user_for_update', lock_user)
        monkeypatch.setattr(apple_iap_module, 'create_apple_transaction', create_apple_txn)
        monkeypatch.setattr(apple_iap_module, 'create_transaction', create_financial_txn)

        result = await service.fulfill_verified_transaction(
            db,
            user_id=1,
            product_id='com.bitnet.vpnclient.topup.100',
            expected_app_account_token='account-token',
            txn_info={
                'transactionId': '2000000123456789',
                'bundleId': 'com.bitnet.vpnclient',
                'productId': 'com.bitnet.vpnclient.topup.100',
                'type': 'Consumable',
                'appAccountToken': 'account-token',
                'environment': 'Sandbox',
            },
        )

        assert result.success is True
        assert result.reason == 'credited'
        assert order == ['lock_user', 'create_apple_transaction', 'create_transaction']
        assert user.balance_kopeks == 11_000
        db.commit.assert_awaited_once()

    @pytest.mark.anyio('asyncio')
    async def test_credit_side_effects_use_injected_bot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
        user = SimpleNamespace(id=1, has_made_first_topup=True, referred_by_id=None)
        transaction = SimpleNamespace(id=42)
        db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
        admin_notifications: list[object] = []
        cart_notifications: list[object] = []

        class StubAdminNotificationService:
            def __init__(self, service_bot):
                admin_notifications.append(service_bot)

            async def send_balance_topup_notification(self, *args, **kwargs):
                return None

        async def send_cart_notification_after_topup(_user, _amount_kopeks, _db, cart_bot):
            cart_notifications.append(cart_bot)

        monkeypatch.setattr(apple_iap_module, 'emit_transaction_side_effects', AsyncMock())
        monkeypatch.setattr('app.services.referral_service.process_referral_topup', AsyncMock())
        monkeypatch.setattr(
            'app.services.admin_notification_service.AdminNotificationService',
            StubAdminNotificationService,
        )
        monkeypatch.setattr(
            'app.services.payment.common.send_cart_notification_after_topup',
            send_cart_notification_after_topup,
        )

        await AppleIAPFulfillmentService(bot=bot)._emit_credit_side_effects(
            db,
            user,
            transaction,
            amount_kopeks=10_000,
            external_id='2000000123456789',
            old_balance=1_000,
            topup_status='Пополнение',
            referrer_info='Нет',
            subscription=None,
            promo_group=None,
            was_first_topup=False,
        )

        assert admin_notifications == [bot]
        assert cart_notifications == [bot]
        bot.session.close.assert_not_awaited()


class TestAdapterFallback:
    @pytest.mark.anyio('asyncio')
    async def test_verify_transaction_can_disable_environment_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _enable_apple_iap(monkeypatch, tmp_path)
        service = AppleIAPService()
        environments: list[object] = []

        class FakeClient:
            async def get_transaction_info(self, transaction_id: str):
                return MagicMock(signedTransactionInfo='signed.txn.info')

            async def async_close(self):
                return None

        monkeypatch.setattr(service, '_client', lambda environment: environments.append(environment) or FakeClient())
        monkeypatch.setattr(service, 'verify_signed_transaction_info', lambda signed, environment=None: None)

        await service.verify_transaction('2000000123456789', 'Production', allow_environment_fallback=False)

        assert len(environments) == 1


class TestNotificationService:
    @pytest.mark.anyio('asyncio')
    async def test_signed_transaction_verification_failure_marks_notification_failed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _enable_apple_iap(monkeypatch, tmp_path)
        db = _FakeDB()
        notification_row = SimpleNamespace(status='received', notification_uuid='notification-uuid')

        class FakeAppleService:
            def verify_notification(self, signed_payload: str):
                return {
                    'notificationUUID': 'notification-uuid',
                    'notificationType': 'REFUND',
                    'data': {'environment': 'Sandbox', 'signedTransactionInfo': 'signed.txn'},
                }

            def verify_signed_transaction_info(self, signed_transaction_info: str, environment: str):
                return None

        monkeypatch.setattr(apple_iap_module, 'AsyncSessionLocal', lambda: _AsyncContext(db))
        monkeypatch.setattr(apple_iap_module, 'get_apple_notification_by_uuid', AsyncMock(return_value=None))
        monkeypatch.setattr(apple_iap_module, 'get_apple_notification_by_payload_hash', AsyncMock(return_value=None))
        monkeypatch.setattr(apple_iap_module, 'create_apple_notification', AsyncMock(return_value=notification_row))
        mark_processed = AsyncMock()
        monkeypatch.setattr(apple_iap_module, 'mark_apple_notification_processed', mark_processed)

        ok, reason = await AppleIAPNotificationService(FakeAppleService()).process_signed_payload(
            'signed.payload',
            b'{"signedPayload":"signed.payload"}',
        )

        assert ok is False
        assert reason == 'signed_transaction_verification_failed'
        mark_processed.assert_awaited_once()
        assert mark_processed.await_args.kwargs['status'] == 'failed'
        db.commit.assert_awaited_once()

    @pytest.mark.anyio('asyncio')
    async def test_refund_uses_outer_notification_environment_for_owner_check(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _FakeDB()
        notification_row = SimpleNamespace(status='received', notification_uuid='notification-uuid')
        apple_txn = SimpleNamespace(
            transaction_id='2000000123456789',
            original_transaction_id='2000000123456789',
            status='verified',
            environment='Sandbox',
            user_id=1,
            amount_kopeks=10_000,
            product_id='com.bitnet.vpnclient.topup.100',
            bundle_id='com.bitnet.vpnclient',
            app_account_token='account-token',
        )

        class FakeAppleService:
            def verify_notification(self, signed_payload: str):
                return {
                    'notificationUUID': 'notification-uuid',
                    'notificationType': 'REFUND',
                    'data': {'environment': 'Production', 'signedTransactionInfo': 'signed.txn'},
                }

            def verify_signed_transaction_info(self, signed_transaction_info: str, environment: str):
                return {
                    'transactionId': '2000000123456789',
                    'originalTransactionId': '2000000123456789',
                    'appAccountToken': 'account-token',
                    'bundleId': 'com.bitnet.vpnclient',
                    'productId': 'com.bitnet.vpnclient.topup.100',
                }

        create_abuse_event = AsyncMock()
        lock_user = AsyncMock()
        monkeypatch.setattr(settings, 'APPLE_IAP_ENVIRONMENT', 'Production', raising=False)
        monkeypatch.setattr(settings, 'APPLE_IAP_ALLOW_SANDBOX_ON_PRODUCTION', False, raising=False)
        monkeypatch.setattr(apple_iap_module, 'AsyncSessionLocal', lambda: _AsyncContext(db))
        monkeypatch.setattr(apple_iap_module, 'get_apple_notification_by_uuid', AsyncMock(return_value=None))
        monkeypatch.setattr(apple_iap_module, 'get_apple_notification_by_payload_hash', AsyncMock(return_value=None))
        monkeypatch.setattr(apple_iap_module, 'create_apple_notification', AsyncMock(return_value=notification_row))
        monkeypatch.setattr(apple_iap_module, 'mark_apple_notification_processed', AsyncMock())
        monkeypatch.setattr(
            apple_iap_module,
            'get_apple_transaction_by_transaction_id_for_update',
            AsyncMock(return_value=apple_txn),
        )
        monkeypatch.setattr(apple_iap_module, 'create_apple_abuse_event', create_abuse_event)
        monkeypatch.setattr(apple_iap_module, 'lock_user_for_pricing', lock_user)

        ok, reason = await AppleIAPNotificationService(FakeAppleService()).process_signed_payload(
            'signed.payload',
            b'{"signedPayload":"signed.payload"}',
        )

        assert ok is True
        assert reason == 'refund_environment_mismatch'
        create_abuse_event.assert_awaited_once()
        lock_user.assert_not_awaited()

    @pytest.mark.anyio('asyncio')
    async def test_duplicate_notification_insert_race_is_treated_as_duplicate(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _enable_apple_iap(monkeypatch, tmp_path)
        db = _FakeDB()
        processed_row = SimpleNamespace(status='processed', notification_uuid='notification-uuid')

        class FakeAppleService:
            def verify_notification(self, signed_payload: str):
                return {
                    'notificationUUID': 'notification-uuid',
                    'notificationType': 'TEST',
                    'data': {'environment': 'Sandbox'},
                }

        get_by_uuid = AsyncMock(side_effect=[None, processed_row])
        create_notification = AsyncMock(side_effect=IntegrityError('insert', {}, Exception('duplicate')))
        monkeypatch.setattr(apple_iap_module, 'AsyncSessionLocal', lambda: _AsyncContext(db))
        monkeypatch.setattr(apple_iap_module, 'get_apple_notification_by_uuid', get_by_uuid)
        monkeypatch.setattr(apple_iap_module, 'get_apple_notification_by_payload_hash', AsyncMock(return_value=None))
        monkeypatch.setattr(apple_iap_module, 'create_apple_notification', create_notification)

        ok, reason = await AppleIAPNotificationService(FakeAppleService()).process_signed_payload(
            'signed.payload',
            b'{"signedPayload":"signed.payload"}',
        )

        assert ok is True
        assert reason == 'duplicate'

    @pytest.mark.anyio('asyncio')
    async def test_notification_payload_hash_replay_is_ignored(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _enable_apple_iap(monkeypatch, tmp_path)
        db = _FakeDB()
        existing_payload = SimpleNamespace(
            status='processed',
            notification_uuid='existing-notification-uuid',
            payload_hash='payload-hash',
        )
        create_notification = AsyncMock()

        class FakeAppleService:
            def verify_notification(self, signed_payload: str):
                return {
                    'notificationUUID': 'new-notification-uuid',
                    'notificationType': 'TEST',
                    'data': {'environment': 'Sandbox'},
                }

        monkeypatch.setattr(apple_iap_module, 'AsyncSessionLocal', lambda: _AsyncContext(db))
        monkeypatch.setattr(apple_iap_module, 'get_apple_notification_by_uuid', AsyncMock(return_value=None))
        monkeypatch.setattr(
            apple_iap_module,
            'get_apple_notification_by_payload_hash',
            AsyncMock(return_value=existing_payload),
        )
        monkeypatch.setattr(apple_iap_module, 'create_apple_notification', create_notification)

        ok, reason = await AppleIAPNotificationService(FakeAppleService()).process_signed_payload(
            'signed.payload',
            b'{"signedPayload":"signed.payload"}',
        )

        assert ok is True
        assert reason == 'payload_replay'
        create_notification.assert_not_awaited()

    @pytest.mark.anyio('asyncio')
    async def test_refund_rejects_app_account_token_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        apple_txn = SimpleNamespace(
            transaction_id='2000000123456789',
            original_transaction_id='2000000123456789',
            status='verified',
            environment='Sandbox',
            user_id=1,
            amount_kopeks=10_000,
            product_id='com.bitnet.vpnclient.topup.100',
            bundle_id='com.bitnet.vpnclient',
            app_account_token='victim-token',
        )
        get_transaction = AsyncMock(return_value=apple_txn)
        create_abuse_event = AsyncMock()
        lock_user = AsyncMock()
        monkeypatch.setattr(settings, 'APPLE_IAP_ENVIRONMENT', 'Sandbox', raising=False)
        monkeypatch.setattr(apple_iap_module, 'get_apple_transaction_by_transaction_id_for_update', get_transaction)
        monkeypatch.setattr(apple_iap_module, 'create_apple_abuse_event', create_abuse_event)
        monkeypatch.setattr(apple_iap_module, 'lock_user_for_pricing', lock_user)

        reason = await AppleIAPNotificationService()._handle_refund(
            _FakeDB(),
            {
                'transactionId': '2000000123456789',
                'originalTransactionId': '2000000123456789',
                'appAccountToken': 'attacker-token',
                'environment': 'Sandbox',
                'bundleId': 'com.bitnet.vpnclient',
                'productId': 'com.bitnet.vpnclient.topup.100',
            },
        )

        assert reason == 'refund_account_token_mismatch'
        create_abuse_event.assert_awaited_once()
        assert create_abuse_event.await_args.args[1] == 'notification_app_account_token_mismatch'
        lock_user.assert_not_awaited()

    @pytest.mark.anyio('asyncio')
    async def test_refund_rejects_environment_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        apple_txn = SimpleNamespace(
            transaction_id='2000000123456789',
            original_transaction_id='2000000123456789',
            status='verified',
            environment='Production',
            user_id=1,
            amount_kopeks=10_000,
            product_id='com.bitnet.vpnclient.topup.100',
            bundle_id='com.bitnet.vpnclient',
            app_account_token='account-token',
        )
        get_transaction = AsyncMock(return_value=apple_txn)
        create_abuse_event = AsyncMock()
        lock_user = AsyncMock()
        monkeypatch.setattr(settings, 'APPLE_IAP_ENVIRONMENT', 'Production', raising=False)
        monkeypatch.setattr(apple_iap_module, 'get_apple_transaction_by_transaction_id_for_update', get_transaction)
        monkeypatch.setattr(apple_iap_module, 'create_apple_abuse_event', create_abuse_event)
        monkeypatch.setattr(apple_iap_module, 'lock_user_for_pricing', lock_user)

        reason = await AppleIAPNotificationService()._handle_refund(
            _FakeDB(),
            {
                'transactionId': '2000000123456789',
                'originalTransactionId': '2000000123456789',
                'appAccountToken': 'account-token',
                'environment': 'Sandbox',
                'bundleId': 'com.bitnet.vpnclient',
                'productId': 'com.bitnet.vpnclient.topup.100',
            },
        )

        assert reason == 'refund_environment_mismatch'
        create_abuse_event.assert_awaited_once()
        assert create_abuse_event.await_args.args[1] == 'notification_environment_mismatch'
        lock_user.assert_not_awaited()

    @pytest.mark.anyio('asyncio')
    async def test_refund_reversed_credits_with_outer_transaction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, 'APPLE_IAP_ENVIRONMENT', 'Sandbox', raising=False)
        apple_txn = SimpleNamespace(
            transaction_id='2000000123456789',
            original_transaction_id='2000000123456789',
            status='refunded',
            environment='Sandbox',
            user_id=1,
            amount_kopeks=10_000,
            product_id='com.bitnet.vpnclient.topup.100',
            bundle_id='com.bitnet.vpnclient',
            app_account_token='account-token',
            refunded_at=object(),
            refund_reversed_at=None,
        )
        add_balance = AsyncMock(return_value=True)
        monkeypatch.setattr(
            apple_iap_module,
            'get_apple_transaction_by_transaction_id_for_update',
            AsyncMock(return_value=apple_txn),
        )
        monkeypatch.setattr('app.database.crud.user.get_user_by_id', AsyncMock(return_value=SimpleNamespace(id=1)))
        monkeypatch.setattr('app.database.crud.user.add_user_balance', add_balance)

        reason = await AppleIAPNotificationService()._handle_refund_reversed(
            _FakeDB(),
            {
                'transactionId': '2000000123456789',
                'originalTransactionId': '2000000123456789',
                'appAccountToken': 'account-token',
                'environment': 'Sandbox',
                'bundleId': 'com.bitnet.vpnclient',
                'productId': 'com.bitnet.vpnclient.topup.100',
            },
        )

        assert reason == 'refund_reversed'
        assert apple_txn.status == 'credited'
        assert apple_txn.refunded_at is None
        add_balance.assert_awaited_once()
        assert add_balance.await_args.kwargs['commit'] is False
        db = add_balance.await_args.kwargs['db']
        db.flush.assert_awaited_once()


class TestAppleIAPRouting:
    @pytest.mark.anyio('asyncio')
    async def test_legacy_aiohttp_webhook_server_does_not_mount_apple_iap(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _enable_apple_iap(monkeypatch, tmp_path)
        monkeypatch.setattr(settings, 'APPLE_IAP_WEBHOOK_PATH', '/apple-iap-webhook', raising=False)

        from app.external.webhook_server import WebhookServer

        app = await WebhookServer(MagicMock()).create_app()
        paths = {route.resource.canonical for route in app.router.routes()}

        assert settings.APPLE_IAP_WEBHOOK_PATH not in paths

    def test_apple_iap_only_router_exposes_only_apple_iap_paths(self) -> None:
        from app.cabinet.apple_iap import apple_iap_only_router

        paths = {route.path for route in apple_iap_only_router.routes}

        assert '/cabinet/apple-iap/account-token' in paths
        assert '/cabinet/apple-purchase' in paths
        assert '/cabinet/admin/apple-iap/transactions' in paths
        assert '/cabinet/admin/apple-iap/reconcile' in paths
        assert not any(path.startswith('/cabinet/subscription') for path in paths)
        assert not any(path.startswith('/cabinet/balance') for path in paths)
        assert not any(path.startswith('/cabinet/admin/users') for path in paths)

    def test_apple_iap_only_router_mounts_cleanly_on_app(self) -> None:
        from fastapi import FastAPI

        from app.cabinet.apple_iap import apple_iap_only_router

        app = FastAPI()
        app.include_router(apple_iap_only_router)

        paths = {route.path for route in app.routes}

        assert '/cabinet/apple-iap/account-token' in paths
        assert '/cabinet/apple-purchase' in paths
        assert not any(path.startswith('/cabinet/subscription') for path in paths)
        assert not any(path.startswith('/cabinet/balance') for path in paths)
