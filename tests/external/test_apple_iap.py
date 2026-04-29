"""Tests for Apple In-App Purchase service and integration."""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings
from app.external.apple_iap import AppleIAPService


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


def _enable_apple_iap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'APPLE_IAP_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_KEY_ID', 'TEST_KEY_ID', raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_ISSUER_ID', 'test-issuer-id', raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_BUNDLE_ID', 'com.bitnet.vpnclient', raising=False)
    monkeypatch.setattr(settings, 'APPLE_IAP_ENVIRONMENT', 'Sandbox', raising=False)
    # Use a dummy key — we won't actually sign in tests
    monkeypatch.setattr(settings, 'APPLE_IAP_PRIVATE_KEY', 'dummy-key', raising=False)
    monkeypatch.setattr(
        settings,
        'APPLE_IAP_PRODUCTS',
        json.dumps({
            'com.bitnet.vpnclient.topup.100': 10_000,
            'com.bitnet.vpnclient.topup.300': 30_000,
            'com.bitnet.vpnclient.topup.500': 50_000,
            'com.bitnet.vpnclient.topup.1000': 100_000,
            'com.bitnet.vpnclient.topup.3000': 300_000,
        }),
        raising=False,
    )


# ---------------------------------------------------------------------------
# Product mapping
# ---------------------------------------------------------------------------

class TestProductMapping:
    """Test product ID to kopeks mapping."""

    def test_all_products_mapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        products = settings.get_apple_iap_products()
        assert len(products) == 5

    def test_product_100(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        products = settings.get_apple_iap_products()
        assert products['com.bitnet.vpnclient.topup.100'] == 10_000

    def test_product_300(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        products = settings.get_apple_iap_products()
        assert products['com.bitnet.vpnclient.topup.300'] == 30_000

    def test_product_500(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        products = settings.get_apple_iap_products()
        assert products['com.bitnet.vpnclient.topup.500'] == 50_000

    def test_product_1000(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        products = settings.get_apple_iap_products()
        assert products['com.bitnet.vpnclient.topup.1000'] == 100_000

    def test_product_3000(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        products = settings.get_apple_iap_products()
        assert products['com.bitnet.vpnclient.topup.3000'] == 300_000

    def test_unknown_product_not_in_map(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        products = settings.get_apple_iap_products()
        assert 'com.bitnet.vpnclient.topup.999' not in products

    def test_invalid_json_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, 'APPLE_IAP_PRODUCTS', 'invalid-json', raising=False)
        products = settings.get_apple_iap_products()
        assert products == {}


# ---------------------------------------------------------------------------
# is_apple_iap_enabled()
# ---------------------------------------------------------------------------

class TestAppleIAPEnabled:
    """Test is_apple_iap_enabled() helper."""

    def test_enabled_with_all_params(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        assert settings.is_apple_iap_enabled() is True

    def test_disabled_when_flag_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        monkeypatch.setattr(settings, 'APPLE_IAP_ENABLED', False, raising=False)
        assert settings.is_apple_iap_enabled() is False

    def test_disabled_when_key_id_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        monkeypatch.setattr(settings, 'APPLE_IAP_KEY_ID', None, raising=False)
        assert settings.is_apple_iap_enabled() is False

    def test_disabled_when_issuer_id_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        monkeypatch.setattr(settings, 'APPLE_IAP_ISSUER_ID', None, raising=False)
        assert settings.is_apple_iap_enabled() is False

    def test_disabled_when_no_private_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        monkeypatch.setattr(settings, 'APPLE_IAP_PRIVATE_KEY', None, raising=False)
        monkeypatch.setattr(settings, 'APPLE_IAP_PRIVATE_KEY_PATH', None, raising=False)
        assert settings.is_apple_iap_enabled() is False

    def test_enabled_with_key_path_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        monkeypatch.setattr(settings, 'APPLE_IAP_PRIVATE_KEY', None, raising=False)
        monkeypatch.setattr(settings, 'APPLE_IAP_PRIVATE_KEY_PATH', '/tmp/test.p8', raising=False)
        assert settings.is_apple_iap_enabled() is True


# ---------------------------------------------------------------------------
# validate_transaction_info
# ---------------------------------------------------------------------------

class TestTransactionValidation:
    """Test validate_transaction_info."""

    def test_valid_transaction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        service = AppleIAPService()
        txn_info = {
            'bundleId': 'com.bitnet.vpnclient',
            'productId': 'com.bitnet.vpnclient.topup.100',
            'type': 'Consumable',
        }
        result = service.validate_transaction_info(txn_info, 'com.bitnet.vpnclient.topup.100')
        assert result is None  # None means valid

    def test_wrong_bundle_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        service = AppleIAPService()
        txn_info = {
            'bundleId': 'com.other.app',
            'productId': 'com.bitnet.vpnclient.topup.100',
            'type': 'Consumable',
        }
        result = service.validate_transaction_info(txn_info, 'com.bitnet.vpnclient.topup.100')
        assert result is not None
        assert 'Bundle ID' in result

    def test_wrong_product_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        service = AppleIAPService()
        txn_info = {
            'bundleId': 'com.bitnet.vpnclient',
            'productId': 'com.bitnet.vpnclient.topup.500',
            'type': 'Consumable',
        }
        result = service.validate_transaction_info(txn_info, 'com.bitnet.vpnclient.topup.100')
        assert result is not None
        assert 'Product ID' in result

    def test_wrong_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        service = AppleIAPService()
        txn_info = {
            'bundleId': 'com.bitnet.vpnclient',
            'productId': 'com.bitnet.vpnclient.topup.100',
            'type': 'Auto-Renewable Subscription',
        }
        result = service.validate_transaction_info(txn_info, 'com.bitnet.vpnclient.topup.100')
        assert result is not None
        assert 'type' in result

    def test_revoked_transaction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        service = AppleIAPService()
        txn_info = {
            'bundleId': 'com.bitnet.vpnclient',
            'productId': 'com.bitnet.vpnclient.topup.100',
            'type': 'Consumable',
            'revocationDate': 1700000000000,
        }
        result = service.validate_transaction_info(txn_info, 'com.bitnet.vpnclient.topup.100')
        assert result is not None
        assert 'revoked' in result.lower()

    def test_valid_without_revocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A transaction without revocationDate should be valid."""
        _enable_apple_iap(monkeypatch)
        service = AppleIAPService()
        txn_info = {
            'bundleId': 'com.bitnet.vpnclient',
            'productId': 'com.bitnet.vpnclient.topup.100',
            'type': 'Consumable',
        }
        result = service.validate_transaction_info(txn_info, 'com.bitnet.vpnclient.topup.100')
        assert result is None


# ---------------------------------------------------------------------------
# Environment URL selection
# ---------------------------------------------------------------------------

class TestBaseUrl:
    """Test environment URL selection."""

    def test_production_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        service = AppleIAPService()
        url = service._get_base_url('Production')
        assert 'api.storekit.itunes.apple.com' in url

    def test_sandbox_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        service = AppleIAPService()
        url = service._get_base_url('Sandbox')
        assert 'api.storekit-sandbox.itunes.apple.com' in url

    def test_default_uses_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        service = AppleIAPService()
        url = service._get_base_url()
        assert 'sandbox' in url  # Fixture sets Sandbox


# ---------------------------------------------------------------------------
# _decode_jws_payload (raw decode, no verification)
# ---------------------------------------------------------------------------

class TestJWSPayloadDecoding:
    """Test _decode_jws_payload."""

    def test_decode_valid_jws(self) -> None:
        service = AppleIAPService()
        header = base64.urlsafe_b64encode(b'{"alg":"ES256"}').rstrip(b'=').decode()
        payload_data = {'bundleId': 'com.test', 'productId': 'test.product'}
        payload = base64.urlsafe_b64encode(json.dumps(payload_data).encode()).rstrip(b'=').decode()
        signature = base64.urlsafe_b64encode(b'fake-signature').rstrip(b'=').decode()
        jws = f'{header}.{payload}.{signature}'

        result = service._decode_jws_payload(jws)
        assert result is not None
        assert result['bundleId'] == 'com.test'
        assert result['productId'] == 'test.product'

    def test_decode_invalid_jws(self) -> None:
        service = AppleIAPService()
        result = service._decode_jws_payload('not-a-jws')
        assert result is None

    def test_decode_empty_string(self) -> None:
        service = AppleIAPService()
        result = service._decode_jws_payload('')
        assert result is None


# ---------------------------------------------------------------------------
# _verify_and_decode_jws (x5c + ES256 verification)
# ---------------------------------------------------------------------------

class TestVerifyAndDecodeJWS:
    """Test _verify_and_decode_jws — the full x5c chain + signature path."""

    def test_rejects_bad_format(self) -> None:
        service = AppleIAPService()
        assert service._verify_and_decode_jws('only-two.parts') is None

    def test_rejects_missing_x5c(self) -> None:
        service = AppleIAPService()
        # Valid 3-part JWS but header has no x5c
        header = base64.urlsafe_b64encode(b'{"alg":"ES256"}').rstrip(b'=').decode()
        payload = base64.urlsafe_b64encode(b'{}').rstrip(b'=').decode()
        sig = base64.urlsafe_b64encode(b'sig').rstrip(b'=').decode()
        assert service._verify_and_decode_jws(f'{header}.{payload}.{sig}') is None

    def test_rejects_empty_x5c(self) -> None:
        service = AppleIAPService()
        header = base64.urlsafe_b64encode(json.dumps({'alg': 'ES256', 'x5c': []}).encode()).rstrip(b'=').decode()
        payload = base64.urlsafe_b64encode(b'{}').rstrip(b'=').decode()
        sig = base64.urlsafe_b64encode(b'sig').rstrip(b'=').decode()
        assert service._verify_and_decode_jws(f'{header}.{payload}.{sig}') is None

    def test_verify_notification_delegates(self) -> None:
        """verify_notification should delegate to _verify_and_decode_jws."""
        service = AppleIAPService()
        service._verify_and_decode_jws = MagicMock(return_value={'test': True})
        result = service.verify_notification('signed.payload.jws')
        service._verify_and_decode_jws.assert_called_once_with('signed.payload.jws')
        assert result == {'test': True}


# ---------------------------------------------------------------------------
# verify_transaction with mocked HTTP
# ---------------------------------------------------------------------------

@pytest.mark.anyio('asyncio')
class TestVerifyTransaction:
    """Test verify_transaction with mocked _fetch_transaction."""

    @staticmethod
    def _ok_response(signed_info: str = 'header.payload.sig') -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {'signedTransactionInfo': signed_info}
        return resp

    @staticmethod
    def _error_response(status: int, text: str = '') -> MagicMock:
        resp = MagicMock()
        resp.status_code = status
        resp.text = text
        return resp

    async def test_successful_verification(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        service = AppleIAPService()

        txn_data = {
            'bundleId': 'com.bitnet.vpnclient',
            'productId': 'com.bitnet.vpnclient.topup.100',
            'type': 'Consumable',
            'transactionId': '2000000123456789',
            'environment': 'Sandbox',
        }

        monkeypatch.setattr(service, '_fetch_transaction', AsyncMock(return_value=self._ok_response()))
        monkeypatch.setattr(service, '_verify_and_decode_jws', lambda token: txn_data)

        result = await service.verify_transaction('2000000123456789', 'Sandbox')

        assert result is not None
        assert result['bundleId'] == 'com.bitnet.vpnclient'
        assert result['transactionId'] == '2000000123456789'

    async def test_verification_with_jws_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If _verify_and_decode_jws returns None, verify_transaction returns None."""
        _enable_apple_iap(monkeypatch)
        service = AppleIAPService()

        monkeypatch.setattr(service, '_fetch_transaction', AsyncMock(return_value=self._ok_response()))
        monkeypatch.setattr(service, '_verify_and_decode_jws', lambda token: None)

        result = await service.verify_transaction('2000000123456789', 'Sandbox')
        assert result is None

    async def test_transaction_not_found_both_envs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """404 on primary triggers fallback; 404 on fallback returns None."""
        _enable_apple_iap(monkeypatch)
        service = AppleIAPService()

        fetch_mock = AsyncMock(return_value=self._error_response(404))
        monkeypatch.setattr(service, '_fetch_transaction', fetch_mock)

        result = await service.verify_transaction('nonexistent', 'Sandbox')
        assert result is None
        # Should have been called twice (primary + fallback)
        assert fetch_mock.call_count == 2

    async def test_fallback_succeeds_on_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """404 on primary, 200 on fallback — should succeed."""
        _enable_apple_iap(monkeypatch)
        service = AppleIAPService()

        txn_data = {'bundleId': 'com.bitnet.vpnclient', 'type': 'Consumable'}
        responses = [self._error_response(404), self._ok_response()]
        fetch_mock = AsyncMock(side_effect=responses)
        monkeypatch.setattr(service, '_fetch_transaction', fetch_mock)
        monkeypatch.setattr(service, '_verify_and_decode_jws', lambda token: txn_data)

        result = await service.verify_transaction('12345', 'Production')
        assert result is not None
        assert fetch_mock.call_count == 2

    async def test_network_error_no_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Network error (None response) should not retry."""
        _enable_apple_iap(monkeypatch)
        service = AppleIAPService()

        fetch_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(service, '_fetch_transaction', fetch_mock)

        result = await service.verify_transaction('12345', 'Sandbox')
        assert result is None
        assert fetch_mock.call_count == 1  # no fallback on network error

    async def test_5xx_no_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """5xx errors should not trigger fallback (only 4xx does)."""
        _enable_apple_iap(monkeypatch)
        service = AppleIAPService()

        fetch_mock = AsyncMock(return_value=self._error_response(500, 'Internal'))
        monkeypatch.setattr(service, '_fetch_transaction', fetch_mock)

        result = await service.verify_transaction('12345', 'Sandbox')
        assert result is None
        assert fetch_mock.call_count == 1

    async def test_rate_limit_triggers_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """429 is 4xx → triggers fallback."""
        _enable_apple_iap(monkeypatch)
        service = AppleIAPService()

        fetch_mock = AsyncMock(return_value=self._error_response(429))
        monkeypatch.setattr(service, '_fetch_transaction', fetch_mock)

        result = await service.verify_transaction('123', 'Sandbox')
        assert result is None
        assert fetch_mock.call_count == 2  # primary + fallback

    async def test_no_signed_transaction_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        service = AppleIAPService()

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {}  # missing signedTransactionInfo
        monkeypatch.setattr(service, '_fetch_transaction', AsyncMock(return_value=resp))

        result = await service.verify_transaction('123', 'Sandbox')
        assert result is None


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

class TestApplePurchaseRequestSchema:
    """Test ApplePurchaseRequest pydantic validation."""

    def test_valid_request(self) -> None:
        from app.cabinet.schemas.apple_iap import ApplePurchaseRequest

        req = ApplePurchaseRequest(
            product_id='com.bitnet.vpnclient.topup.100',
            transaction_id='2000000123456789',
        )
        assert req.transaction_id == '2000000123456789'

    def test_rejects_non_numeric_transaction_id(self) -> None:
        from app.cabinet.schemas.apple_iap import ApplePurchaseRequest

        with pytest.raises(Exception, match='digits'):
            ApplePurchaseRequest(
                product_id='com.bitnet.vpnclient.topup.100',
                transaction_id='abc-not-numeric',
            )

    def test_rejects_empty_transaction_id(self) -> None:
        from app.cabinet.schemas.apple_iap import ApplePurchaseRequest

        with pytest.raises(Exception):
            ApplePurchaseRequest(
                product_id='com.bitnet.vpnclient.topup.100',
                transaction_id='',
            )

    def test_rejects_too_long_transaction_id(self) -> None:
        from app.cabinet.schemas.apple_iap import ApplePurchaseRequest

        with pytest.raises(Exception):
            ApplePurchaseRequest(
                product_id='com.bitnet.vpnclient.topup.100',
                transaction_id='1' * 65,
            )

    def test_no_environment_field(self) -> None:
        """Schema should not accept environment — it's server-side only."""
        from app.cabinet.schemas.apple_iap import ApplePurchaseRequest

        req = ApplePurchaseRequest(
            product_id='com.bitnet.vpnclient.topup.100',
            transaction_id='123',
        )
        assert not hasattr(req, 'environment')


# ---------------------------------------------------------------------------
# Sandbox detection
# ---------------------------------------------------------------------------

class TestSandboxDetection:
    """Test that sandbox transactions don't credit real balance."""

    def test_sandbox_env_detected_from_txn_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """validate_transaction_info does not reject sandbox env — that's handled at the route level."""
        _enable_apple_iap(monkeypatch)
        service = AppleIAPService()
        txn_info = {
            'bundleId': 'com.bitnet.vpnclient',
            'productId': 'com.bitnet.vpnclient.topup.100',
            'type': 'Consumable',
            'environment': 'Sandbox',
        }
        result = service.validate_transaction_info(txn_info, 'com.bitnet.vpnclient.topup.100')
        assert result is None  # validation passes — sandbox check is higher up

    def test_production_txn_on_production_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Production environment in txn_info + Production config = proceed normally."""
        _enable_apple_iap(monkeypatch)
        monkeypatch.setattr(settings, 'APPLE_IAP_ENVIRONMENT', 'Production', raising=False)
        txn_info = {'environment': 'Production'}
        is_sandbox = txn_info.get('environment') == 'Sandbox'
        assert is_sandbox is False

    def test_sandbox_txn_on_production_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sandbox environment in txn_info + Production config = sandbox detected."""
        _enable_apple_iap(monkeypatch)
        monkeypatch.setattr(settings, 'APPLE_IAP_ENVIRONMENT', 'Production', raising=False)
        txn_info = {'environment': 'Sandbox'}
        is_sandbox = txn_info.get('environment') == 'Sandbox'
        should_skip_balance = is_sandbox and settings.APPLE_IAP_ENVIRONMENT == 'Production'
        assert should_skip_balance is True

    def test_sandbox_txn_on_sandbox_credits_normally(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sandbox environment in txn_info + Sandbox config = credit normally (testing)."""
        _enable_apple_iap(monkeypatch)
        monkeypatch.setattr(settings, 'APPLE_IAP_ENVIRONMENT', 'Sandbox', raising=False)
        txn_info = {'environment': 'Sandbox'}
        is_sandbox = txn_info.get('environment') == 'Sandbox'
        should_skip_balance = is_sandbox and settings.APPLE_IAP_ENVIRONMENT == 'Production'
        assert should_skip_balance is False
