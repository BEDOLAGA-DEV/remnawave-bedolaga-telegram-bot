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


class TestJWSPayloadDecoding:
    """Test _decode_jws_payload."""

    def test_decode_valid_jws(self) -> None:
        service = AppleIAPService()
        # Build a fake JWS with a JSON payload
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


@pytest.mark.anyio('asyncio')
class TestVerifyTransaction:
    """Test verify_transaction with mocked HTTP."""

    async def test_successful_verification(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        service = AppleIAPService()

        # Build a fake signed transaction info JWS
        header = base64.urlsafe_b64encode(b'{"alg":"ES256"}').rstrip(b'=').decode()
        txn_data = {
            'bundleId': 'com.bitnet.vpnclient',
            'productId': 'com.bitnet.vpnclient.topup.100',
            'type': 'Consumable',
            'transactionId': '2000000123456789',
            'environment': 'Sandbox',
        }
        payload = base64.urlsafe_b64encode(json.dumps(txn_data).encode()).rstrip(b'=').decode()
        sig = base64.urlsafe_b64encode(b'sig').rstrip(b'=').decode()
        signed_txn_info = f'{header}.{payload}.{sig}'

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'signedTransactionInfo': signed_txn_info}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        # Mock JWT generation to avoid needing a real key
        monkeypatch.setattr(service, '_generate_jwt', lambda: 'fake-jwt')

        with patch('app.external.apple_iap.httpx.AsyncClient', return_value=mock_client):
            result = await service.verify_transaction('2000000123456789', 'Sandbox')

        assert result is not None
        assert result['bundleId'] == 'com.bitnet.vpnclient'
        assert result['productId'] == 'com.bitnet.vpnclient.topup.100'

    async def test_transaction_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        service = AppleIAPService()

        mock_response = MagicMock()
        mock_response.status_code = 404

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        monkeypatch.setattr(service, '_generate_jwt', lambda: 'fake-jwt')

        with patch('app.external.apple_iap.httpx.AsyncClient', return_value=mock_client):
            result = await service.verify_transaction('nonexistent', 'Sandbox')

        assert result is None

    async def test_auth_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_apple_iap(monkeypatch)
        service = AppleIAPService()

        mock_response = MagicMock()
        mock_response.status_code = 401

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        monkeypatch.setattr(service, '_generate_jwt', lambda: 'fake-jwt')

        with patch('app.external.apple_iap.httpx.AsyncClient', return_value=mock_client):
            result = await service.verify_transaction('123', 'Sandbox')

        assert result is None
