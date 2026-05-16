"""Apple App Store Server API integration backed by Apple's official library."""

from __future__ import annotations

import datetime as dt
import hashlib
from enum import Enum
from typing import Any
from uuid import UUID

import attrs
import structlog
from appstoreserverlibrary.api_client import APIException, AsyncAppStoreServerAPIClient
from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier, VerificationException

from app.config import settings


logger = structlog.get_logger(__name__)


class AppleIAPConfigurationError(RuntimeError):
    """Raised when Apple IAP settings are incomplete or invalid."""


def _apple_environment(environment: str | None = None) -> Environment:
    configured = environment or settings.get_apple_iap_environment()
    return Environment.SANDBOX if configured == 'Sandbox' else Environment.PRODUCTION


def _opposite_environment(environment: Environment) -> Environment:
    return Environment.SANDBOX if environment == Environment.PRODUCTION else Environment.PRODUCTION


def _environment_name(environment: Environment | str | None) -> str | None:
    if environment is None:
        return None
    if isinstance(environment, Environment):
        return 'Sandbox' if environment == Environment.SANDBOX else 'Production'
    if environment == 'Sandbox':
        return 'Sandbox'
    if environment == 'Production':
        return 'Production'
    return str(environment)


def _hash_token(token: str | None) -> str | None:
    if not token:
        return None
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _primitive(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if attrs.has(value.__class__):
        return _model_to_dict(value)
    if isinstance(value, list):
        return [_primitive(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _primitive(item) for key, item in value.items()}
    return value


def _model_to_dict(model: Any) -> dict[str, Any]:
    if model is None:
        return {}
    if isinstance(model, dict):
        return {str(key): _primitive(value) for key, value in model.items()}
    if attrs.has(model.__class__):
        return {field.name: _primitive(getattr(model, field.name, None)) for field in attrs.fields(model.__class__)}
    return {
        key: _primitive(value) for key, value in vars(model).items() if not key.startswith('_') and value is not None
    }


def parse_apple_timestamp(value: Any) -> dt.datetime | None:
    """Convert Apple millisecond timestamps or ISO strings to aware UTC datetimes."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, str):
        if value.isdigit():
            value = int(value)
        else:
            try:
                parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    if isinstance(value, int | float):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return dt.datetime.fromtimestamp(timestamp, tz=dt.UTC)
    return None


class AppleIAPService:
    """Library-backed adapter for App Store Server API and signed payload verification."""

    def __init__(self):
        self._root_certificate_cache: list[bytes] | None = None

    def _private_key_bytes(self) -> bytes:
        private_key = settings.get_apple_iap_private_key()
        if not private_key:
            raise AppleIAPConfigurationError('Apple IAP private key is not configured')
        return private_key.encode('utf-8')

    def _root_certificates(self) -> list[bytes]:
        if self._root_certificate_cache is not None:
            return self._root_certificate_cache

        certificates: list[bytes] = []
        for cert_path in settings.get_apple_iap_root_cert_paths():
            try:
                certificates.append(cert_path.read_bytes())
            except OSError as error:
                raise AppleIAPConfigurationError(f'Apple root certificate is not readable: {cert_path}') from error
        if not certificates:
            raise AppleIAPConfigurationError('Apple root certificates are not configured')
        self._root_certificate_cache = certificates
        return certificates

    def _client(self, environment: Environment) -> AsyncAppStoreServerAPIClient:
        key_id = settings.APPLE_IAP_KEY_ID
        issuer_id = settings.APPLE_IAP_ISSUER_ID
        if not key_id or not issuer_id:
            raise AppleIAPConfigurationError('Apple IAP key ID or issuer ID is not configured')
        return AsyncAppStoreServerAPIClient(
            self._private_key_bytes(),
            key_id,
            issuer_id,
            settings.APPLE_IAP_BUNDLE_ID,
            environment,
        )

    def _verifier(self, environment: Environment | None = None) -> SignedDataVerifier:
        env = environment or _apple_environment()
        app_apple_id = settings.APPLE_IAP_APP_APPLE_ID if env == Environment.PRODUCTION else None
        return SignedDataVerifier(
            self._root_certificates(),
            settings.APPLE_IAP_ENABLE_ONLINE_CERT_CHECKS,
            env,
            settings.APPLE_IAP_BUNDLE_ID,
            app_apple_id,
        )

    async def verify_transaction(
        self,
        transaction_id: str,
        environment: str | None = None,
        *,
        allow_environment_fallback: bool = True,
    ) -> dict[str, Any] | None:
        """Fetch a transaction from Apple and verify the returned signedTransactionInfo."""
        primary = _apple_environment(environment)
        environments = (primary, _opposite_environment(primary)) if allow_environment_fallback else (primary,)
        for attempt_env in environments:
            client = self._client(attempt_env)
            try:
                response = await client.get_transaction_info(transaction_id)
            except APIException as error:
                await client.async_close()
                status = getattr(error, 'http_status_code', None)
                if allow_environment_fallback and attempt_env == primary and status is not None and 400 <= status < 500:
                    logger.info(
                        'Apple transaction lookup failed on primary environment, retrying fallback',
                        transaction_id=transaction_id,
                        status=status,
                        environment=_environment_name(attempt_env),
                    )
                    continue
                self._log_api_exception(error, transaction_id)
                return None
            except Exception as error:
                await client.async_close()
                logger.error(
                    'Apple transaction lookup failed',
                    transaction_id=transaction_id,
                    error=str(error),
                    exc_info=True,
                )
                return None
            else:
                await client.async_close()

            signed_transaction_info = getattr(response, 'signedTransactionInfo', None)
            if not signed_transaction_info:
                logger.warning(
                    'Apple transaction response missing signedTransactionInfo', transaction_id=transaction_id
                )
                return None

            decoded = self.verify_signed_transaction_info(signed_transaction_info, _environment_name(attempt_env))
            if decoded:
                decoded['signedTransactionInfoHash'] = _hash_token(signed_transaction_info)
                return decoded
            return None

        return None

    def verify_signed_transaction_info(
        self,
        signed_transaction_info: str,
        environment: str | None = None,
    ) -> dict[str, Any] | None:
        environments = [_apple_environment(environment)]
        if environment is None:
            environments.append(_opposite_environment(environments[0]))

        for env in environments:
            try:
                verifier = self._verifier(env)
                decoded = verifier.verify_and_decode_signed_transaction(signed_transaction_info)
                return _model_to_dict(decoded)
            except VerificationException as error:
                logger.warning(
                    'Apple signed transaction verification failed',
                    status=str(getattr(error, 'status', 'unknown')),
                    environment=_environment_name(env),
                )
                continue
            except AppleIAPConfigurationError:
                raise
            except Exception as error:
                logger.error('Apple signed transaction verification error', error=str(error), exc_info=True)
                return None
        return None

    def verify_notification(self, signed_payload: str, environment: str | None = None) -> dict[str, Any] | None:
        environments = [_apple_environment(environment)]
        if environment is None:
            environments.append(_opposite_environment(environments[0]))

        for env in environments:
            try:
                verifier = self._verifier(env)
                decoded = verifier.verify_and_decode_notification(signed_payload)
                notification = _model_to_dict(decoded)
                notification['signedPayloadHash'] = _hash_token(signed_payload)
                return notification
            except VerificationException as error:
                logger.warning(
                    'Apple notification verification failed',
                    status=str(getattr(error, 'status', 'unknown')),
                    environment=_environment_name(env),
                )
                continue
            except AppleIAPConfigurationError:
                raise
            except Exception as error:
                logger.error('Apple notification verification error', error=str(error), exc_info=True)
                return None
        return None

    def validate_transaction_info(self, txn_info: dict[str, Any], expected_product_id: str) -> str | None:
        bundle_id = txn_info.get('bundleId')
        if bundle_id != settings.APPLE_IAP_BUNDLE_ID:
            return f'Bundle ID mismatch: {bundle_id}'

        product_id = txn_info.get('productId')
        if product_id != expected_product_id:
            return f'Product ID mismatch: {product_id} != {expected_product_id}'

        txn_type = txn_info.get('type') or txn_info.get('rawType')
        if txn_type != 'Consumable':
            return f'Unexpected transaction type: {txn_type}'

        if txn_info.get('revocationDate'):
            return f'Transaction was revoked at {txn_info["revocationDate"]}'

        return None

    @staticmethod
    def _log_api_exception(error: APIException, transaction_id: str | None = None) -> None:
        status = getattr(error, 'http_status_code', None)
        if status == 401:
            logger.error('Apple API auth failed -- check key configuration', transaction_id=transaction_id)
        elif status == 404:
            logger.warning('Apple transaction not found', transaction_id=transaction_id)
        elif status == 429:
            logger.warning('Apple API rate limit exceeded', transaction_id=transaction_id)
        else:
            logger.error(
                'Apple API error',
                transaction_id=transaction_id,
                status=status,
                raw_api_error=getattr(error, 'raw_api_error', None),
                error_message=getattr(error, 'error_message', None),
            )
