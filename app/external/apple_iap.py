"""Apple App Store Server API client for In-App Purchase verification and webhook handling."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx
import jwt as pyjwt
import structlog
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.x509 import load_der_x509_certificate

from app.config import settings


logger = structlog.get_logger(__name__)

# Apple Root CA certificates (G3) fingerprint for chain validation
APPLE_ROOT_CA_G3_SUBJECT_CN = 'Apple Root CA - G3'

PRODUCTION_BASE_URL = 'https://api.storekit.itunes.apple.com'
SANDBOX_BASE_URL = 'https://api.storekit-sandbox.itunes.apple.com'


class AppleIAPService:
    """Service for verifying Apple In-App Purchase transactions and handling notifications."""

    def _get_base_url(self, environment: str | None = None) -> str:
        env = environment or settings.APPLE_IAP_ENVIRONMENT
        if env == 'Sandbox':
            return SANDBOX_BASE_URL
        return PRODUCTION_BASE_URL

    def _generate_jwt(self) -> str:
        """Generate a fresh ES256 JWT for App Store Server API authentication.

        Apple recommends generating a new JWT for each request.
        """
        private_key = settings.get_apple_iap_private_key()
        if not private_key:
            raise ValueError('Apple IAP private key is not configured')

        now = int(time.time())
        payload = {
            'iss': settings.APPLE_IAP_ISSUER_ID,
            'iat': now,
            'exp': now + 3600,
            'aud': 'appstoreconnect-v1',
            'bid': settings.APPLE_IAP_BUNDLE_ID,
        }
        headers = {
            'alg': 'ES256',
            'kid': settings.APPLE_IAP_KEY_ID,
            'typ': 'JWT',
        }

        return pyjwt.encode(payload, private_key, algorithm='ES256', headers=headers)

    async def verify_transaction(
        self, transaction_id: str, environment: str | None = None
    ) -> dict[str, Any] | None:
        """Verify a transaction with Apple's App Store Server API.

        Returns the decoded transaction info or None on failure.
        """
        base_url = self._get_base_url(environment)
        url = f'{base_url}/inApps/v1/transactions/{transaction_id}'
        token = self._generate_jwt()

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(
                    url,
                    headers={'Authorization': f'Bearer {token}'},
                )
            except httpx.RequestError as e:
                logger.error('Apple API request failed', error=str(e), transaction_id=transaction_id)
                return None

        if response.status_code == 200:
            data = response.json()
            signed_transaction_info = data.get('signedTransactionInfo')
            if signed_transaction_info:
                decoded = self._decode_jws_payload(signed_transaction_info)
                if decoded:
                    return decoded
                logger.warning('Failed to decode signedTransactionInfo', transaction_id=transaction_id)
                return None
            logger.warning('No signedTransactionInfo in response', transaction_id=transaction_id)
            return None

        if response.status_code == 404:
            logger.warning('Apple transaction not found', transaction_id=transaction_id)
        elif response.status_code == 401:
            logger.error('Apple API auth failed — check key configuration')
        elif response.status_code == 429:
            logger.warning('Apple API rate limit exceeded')
        else:
            logger.error(
                'Apple API unexpected status',
                status=response.status_code,
                body=response.text[:500],
                transaction_id=transaction_id,
            )

        return None

    def validate_transaction_info(self, txn_info: dict[str, Any], expected_product_id: str) -> str | None:
        """Validate decoded transaction info fields.

        Returns None if valid, or an error message string.
        """
        bundle_id = txn_info.get('bundleId')
        if bundle_id != settings.APPLE_IAP_BUNDLE_ID:
            return f'Bundle ID mismatch: {bundle_id}'

        product_id = txn_info.get('productId')
        if product_id != expected_product_id:
            return f'Product ID mismatch: {product_id} != {expected_product_id}'

        txn_type = txn_info.get('type')
        if txn_type != 'Consumable':
            return f'Unexpected transaction type: {txn_type}'

        return None

    def verify_notification(self, signed_payload: str) -> dict[str, Any] | None:
        """Verify and decode an App Store Server Notification V2 payload.

        Verifies the JWS x5c certificate chain, then returns the decoded payload.
        Returns None if verification fails.
        """
        try:
            # Split JWS into parts
            parts = signed_payload.split('.')
            if len(parts) != 3:
                logger.warning('Invalid JWS format: expected 3 parts')
                return None

            # Decode header to get x5c chain
            header_b64 = parts[0]
            # Add padding if necessary
            padding = 4 - len(header_b64) % 4
            if padding != 4:
                header_b64 += '=' * padding
            header_json = base64.urlsafe_b64decode(header_b64)
            header = json.loads(header_json)

            x5c_chain = header.get('x5c', [])
            if not x5c_chain:
                logger.warning('No x5c certificate chain in JWS header')
                return None

            # Verify the certificate chain
            if not self._verify_x5c_chain(x5c_chain):
                logger.warning('x5c certificate chain verification failed')
                return None

            # Verify the signature using the leaf certificate
            leaf_cert_der = base64.b64decode(x5c_chain[0])
            leaf_cert = load_der_x509_certificate(leaf_cert_der)
            public_key = leaf_cert.public_key()

            # Verify JWS signature
            signing_input = f'{parts[0]}.{parts[1]}'.encode('ascii')
            signature_b64 = parts[2]
            sig_padding = 4 - len(signature_b64) % 4
            if sig_padding != 4:
                signature_b64 += '=' * sig_padding
            signature = base64.urlsafe_b64decode(signature_b64)

            # ES256 signatures from JWS are in raw (r||s) format, convert to DER
            if len(signature) == 64:
                r = int.from_bytes(signature[:32], 'big')
                s = int.from_bytes(signature[32:], 'big')
                signature = asym_utils.encode_dss_signature(r, s)

            public_key.verify(signature, signing_input, ec.ECDSA(SHA256()))

            # Decode payload
            return self._decode_jws_payload(signed_payload)

        except Exception as e:
            logger.error('Failed to verify Apple notification', error=str(e), exc_info=True)
            return None

    def _verify_x5c_chain(self, x5c_chain: list[str]) -> bool:
        """Verify the x5c certificate chain ends with an Apple Root CA."""
        try:
            if len(x5c_chain) < 2:
                logger.warning('x5c chain too short', length=len(x5c_chain))
                return False

            certs = []
            for cert_b64 in x5c_chain:
                cert_der = base64.b64decode(cert_b64)
                cert = load_der_x509_certificate(cert_der)
                certs.append(cert)

            # Check the root (last) certificate is Apple's
            root_cert = certs[-1]
            root_cn = None
            for attr in root_cert.subject:
                if attr.oid == x509.oid.NameOID.COMMON_NAME:
                    root_cn = attr.value
                    break

            if root_cn != APPLE_ROOT_CA_G3_SUBJECT_CN:
                logger.warning('Root CA is not Apple Root CA - G3', root_cn=root_cn)
                return False

            # Verify each certificate is signed by the next one in the chain
            for i in range(len(certs) - 1):
                child = certs[i]
                parent = certs[i + 1]
                parent_public_key = parent.public_key()
                parent_public_key.verify(
                    child.signature,
                    child.tbs_certificate_bytes,
                    ec.ECDSA(child.signature_hash_algorithm),
                )

            return True

        except Exception as e:
            logger.error('x5c chain verification error', error=str(e))
            return False

    def _decode_jws_payload(self, jws_token: str) -> dict[str, Any] | None:
        """Decode the payload from a JWS token without signature verification.

        Use only after the signature has already been verified.
        """
        try:
            parts = jws_token.split('.')
            if len(parts) != 3:
                return None

            payload_b64 = parts[1]
            # Add base64url padding
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += '=' * padding

            payload_json = base64.urlsafe_b64decode(payload_b64)
            return json.loads(payload_json)

        except Exception as e:
            logger.error('Failed to decode JWS payload', error=str(e))
            return None

    async def send_consumption_info(
        self,
        transaction_id: str,
        customer_consented: bool,
        consumption_status: int = 0,
        delivery_status: int = 0,
        lifetime_dollars_purchased: int = 0,
        lifetime_dollars_refunded: int = 0,
        platform: int = 1,
        play_time: int = 0,
        sample_content_provided: bool = False,
        user_status: int = 0,
        environment: str | None = None,
        refund_preference: int | None = None,
    ) -> bool:
        """Send consumption information to Apple in response to CONSUMPTION_REQUEST.

        Must be sent within 12 hours of receiving the notification.
        """
        base_url = self._get_base_url(environment)
        url = f'{base_url}/inApps/v2/transactions/consumption/{transaction_id}'
        token = self._generate_jwt()

        body: dict[str, Any] = {
            'customerConsented': customer_consented,
            'consumptionStatus': consumption_status,
            'deliveryStatus': delivery_status,
            'lifetimeDollarsPurchased': lifetime_dollars_purchased,
            'lifetimeDollarsRefunded': lifetime_dollars_refunded,
            'platform': platform,
            'playTime': play_time,
            'sampleContentProvided': sample_content_provided,
            'userStatus': user_status,
        }
        if refund_preference is not None:
            body['refundPreference'] = refund_preference

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.put(
                    url,
                    json=body,
                    headers={
                        'Authorization': f'Bearer {token}',
                        'Content-Type': 'application/json',
                    },
                )
            except httpx.RequestError as e:
                logger.error('Apple consumption API request failed', error=str(e))
                return False

        if response.status_code == 202:
            logger.info('Consumption info sent to Apple', transaction_id=transaction_id)
            return True

        logger.error(
            'Apple consumption API error',
            status=response.status_code,
            body=response.text[:500],
            transaction_id=transaction_id,
        )
        return False
