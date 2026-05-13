"""Сервис для работы с API Overpay (pay.overpay.io)."""

import ssl
import tempfile
from typing import Any

import httpx
import structlog
from cryptography.hazmat.primitives.serialization import (
    BestAvailableEncryption,
    Encoding,
    NoEncryption,
    PrivateFormat,
    pkcs12,
)

from app.config import settings


logger = structlog.get_logger(__name__)


class OverpayAPIError(Exception):
    """Ошибка API Overpay."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f'Overpay API error ({status_code}): {message}')


class OverpayService:
    """Сервис для работы с API Overpay.

    Overpay использует HTTP Basic Auth + mTLS (P12 сертификат).
    """

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._ssl_context: ssl.SSLContext | None = None
        self._temp_cert_file: str | None = None
        self._temp_key_file: str | None = None

    @property
    def api_url(self) -> str:
        return (settings.OVERPAY_API_URL or 'https://api.overpay.io').rstrip('/')

    @property
    def username(self) -> str:
        return settings.OVERPAY_USERNAME or ''

    @property
    def password(self) -> str:
        return settings.OVERPAY_PASSWORD or ''

    @property
    def project_id(self) -> str:
        return settings.OVERPAY_PROJECT_ID or ''

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        """Создает SSL контекст с P12 сертификатом для mTLS."""
        p12_path = settings.OVERPAY_P12_PATH
        if not p12_path:
            return None

        if self._ssl_context is not None:
            return self._ssl_context

        try:
            passphrase = settings.OVERPAY_P12_PASSPHRASE
            passphrase_bytes = passphrase.encode('utf-8') if passphrase else None

            with open(p12_path, 'rb') as f:
                p12_data = f.read()

            private_key, certificate, additional_certs = pkcs12.load_key_and_certificates(p12_data, passphrase_bytes)

            # Write PEM files to temp files for ssl.SSLContext
            cert_pem = certificate.public_bytes(Encoding.PEM)
            if additional_certs:
                for cert in additional_certs:
                    cert_pem += cert.public_bytes(Encoding.PEM)

            if passphrase_bytes:
                key_pem = private_key.private_bytes(
                    Encoding.PEM,
                    PrivateFormat.TraditionalOpenSSL,
                    BestAvailableEncryption(passphrase_bytes),
                )
            else:
                key_pem = private_key.private_bytes(
                    Encoding.PEM,
                    PrivateFormat.TraditionalOpenSSL,
                    NoEncryption(),
                )

            # Write to temp files
            with tempfile.NamedTemporaryFile(delete=False, suffix='.pem') as cert_file:
                cert_file.write(cert_pem)
                cert_file.flush()
                self._temp_cert_file = cert_file.name

            with tempfile.NamedTemporaryFile(delete=False, suffix='.pem') as key_file:
                key_file.write(key_pem)
                key_file.flush()
                self._temp_key_file = key_file.name

            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.load_cert_chain(
                certfile=self._temp_cert_file,
                keyfile=self._temp_key_file,
                password=passphrase,
            )
            ctx.load_default_certs()
            self._ssl_context = ctx
            logger.info('Overpay: SSL контекст с P12 сертификатом создан')
            return ctx

        except Exception as e:
            logger.exception('Overpay: ошибка загрузки P12 сертификата', error=e)
            return None

    async def _get_client(self) -> httpx.AsyncClient:
        """Возвращает переиспользуемый HTTP-клиент с mTLS."""
        if self._client is not None and not self._client.is_closed:
            return self._client

        ssl_context = self._build_ssl_context()

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            auth=httpx.BasicAuth(self.username, self.password),
            verify=ssl_context if ssl_context else True,
        )
        return self._client

    async def close(self) -> None:
        """Закрывает HTTP-клиент."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

        # Clean up temp files
        from pathlib import Path

        for path in (self._temp_cert_file, self._temp_key_file):
            if path:
                try:
                    Path(path).unlink()
                except OSError:
                    pass
        self._temp_cert_file = None
        self._temp_key_file = None
        self._ssl_context = None

    async def create_payment(
        self,
        *,
        amount: str,
        currency: str = 'RUB',
        lifetime_minutes: int = 1440,
        merchant_transaction_id: str,
        description: str = '',
        return_url: str | None = None,
        payment_methods: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Создает платеж через API Overpay.
        POST {API_URL}/orders/
        """
        payload: dict[str, Any] = {
            'amount': amount,
            'currency': currency,
            'livetimeMinutes': lifetime_minutes,
            'projectId': self.project_id,
            'merchantTransactionId': merchant_transaction_id,
        }

        if description:
            payload['description'] = description
        if return_url:
            payload['returnUrl'] = return_url
        if payment_methods:
            payload['paymentMethods'] = payment_methods

        logger.info(
            'Overpay API create_payment',
            merchant_transaction_id=merchant_transaction_id,
            amount=amount,
            currency=currency,
        )

        try:
            client = await self._get_client()
            response = await client.post(
                f'{self.api_url}/orders/',
                json=payload,
                headers={'Content-Type': 'application/json'},
            )

            data = response.json()

            if response.status_code == 200 or response.status_code == 201:
                logger.info(
                    'Overpay API payment created',
                    merchant_transaction_id=merchant_transaction_id,
                    overpay_id=data.get('id'),
                    result_url=data.get('resultUrl'),
                )
                return data

            error_msg = data.get('message') or data.get('error') or str(data)
            logger.error(
                'Overpay create_payment error',
                status_code=response.status_code,
                error_msg=error_msg,
                response_data=data,
            )
            raise OverpayAPIError(response.status_code, error_msg)

        except httpx.HTTPError as e:
            logger.exception('Overpay API connection error', error=e)
            raise

    async def get_payment(self, order_id: str) -> dict[str, Any]:
        """
        Получает информацию о платеже по ID.
        GET {API_URL}/orders/{id}
        """
        logger.info('Overpay get_payment', order_id=order_id)

        try:
            client = await self._get_client()
            response = await client.get(
                f'{self.api_url}/orders/{order_id}',
                headers={'Content-Type': 'application/json'},
            )

            data = response.json()

            if response.status_code == 200:
                return data

            error_msg = data.get('message') or data.get('error') or str(data)
            logger.error(
                'Overpay get_payment error',
                status_code=response.status_code,
                error_msg=error_msg,
            )
            raise OverpayAPIError(response.status_code, error_msg)

        except httpx.HTTPError as e:
            logger.exception('Overpay API connection error', error=e)
            raise

    async def refund_payment(self, order_id: str, amount: str) -> dict[str, Any]:
        """
        Возврат платежа.
        PUT {API_URL}/orders/{id}/refund
        """
        logger.info('Overpay refund_payment', order_id=order_id, amount=amount)

        try:
            client = await self._get_client()
            response = await client.put(
                f'{self.api_url}/orders/{order_id}/refund',
                json={'amount': amount},
                headers={'Content-Type': 'application/json'},
            )

            data = response.json()

            if response.status_code == 200:
                logger.info('Overpay refund successful', order_id=order_id, amount=amount)
                return data

            error_msg = data.get('message') or data.get('error') or str(data)
            logger.error(
                'Overpay refund error',
                status_code=response.status_code,
                error_msg=error_msg,
            )
            raise OverpayAPIError(response.status_code, error_msg)

        except httpx.HTTPError as e:
            logger.exception('Overpay API connection error', error=e)
            raise


# Singleton instance
overpay_service = OverpayService()
