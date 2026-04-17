"""Сервис для работы с API RollyPay (rollypay.io)."""

import hashlib
import hmac
import uuid
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

API_BASE_URL = 'https://rollypay.io/api/v1'


class RollyPayAPIError(Exception):
    """Ошибка API RollyPay."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f'RollyPay API error ({status_code}): {message}')


class RollyPayService:
    """Сервис для работы с API RollyPay."""

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    @property
    def api_key(self) -> str:
        return settings.ROLLYPAY_API_KEY or ''

    @property
    def signing_secret(self) -> str:
        return settings.ROLLYPAY_SIGNING_SECRET or ''

    async def _get_session(self) -> aiohttp.ClientSession:
        """Возвращает переиспользуемую HTTP-сессию."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self) -> None:
        """Закрывает HTTP-сессию."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _build_headers(self) -> dict[str, str]:
        """Строит заголовки запроса с X-API-Key и X-Nonce."""
        return {
            'Content-Type': 'application/json',
            'X-API-Key': self.api_key,
            'X-Nonce': str(uuid.uuid4()),
        }

    async def create_payment(
        self,
        *,
        amount_value: str,
        currency: str = 'RUB',
        order_id: str,
        payment_method: str | None = None,
        description: str = '',
        redirect_url: str | None = None,
        success_redirect_url: str | None = None,
        fail_redirect_url: str | None = None,
        customer_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Создает платеж через API RollyPay.
        POST /api/v1/payments
        """
        payload: dict[str, Any] = {
            'amount': amount_value,
            'payment_currency': currency,
            'order_id': order_id,
        }
        if payment_method:
            payload['payment_method'] = payment_method

        if description:
            payload['description'] = description
        if redirect_url:
            payload['redirect_url'] = redirect_url
        if success_redirect_url:
            payload['success_redirect_url'] = success_redirect_url
        if fail_redirect_url:
            payload['fail_redirect_url'] = fail_redirect_url
        if customer_id:
            payload['customer_id'] = customer_id
        if metadata:
            payload['metadata'] = metadata

        logger.info(
            'RollyPay API create_payment',
            order_id=order_id,
            amount=amount_value,
            currency=currency,
            payment_method=payment_method,
        )

        try:
            session = await self._get_session()
            async with session.post(
                f'{API_BASE_URL}/payments',
                json=payload,
                headers=self._build_headers(),
            ) as response:
                data = await response.json(content_type=None)

                if response.status == 200:
                    logger.info(
                        'RollyPay API payment created',
                        order_id=order_id,
                        payment_id=data.get('payment_id'),
                        pay_url=data.get('pay_url'),
                    )
                    return data

                error_msg = data.get('message') or data.get('error') or str(data)
                logger.error(
                    'RollyPay create_payment error',
                    status_code=response.status,
                    error_msg=error_msg,
                    response_data=data,
                )
                raise RollyPayAPIError(response.status, error_msg)

        except aiohttp.ClientError as e:
            logger.exception('RollyPay API connection error', error=e)
            raise

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        """
        Получает информацию о платеже по ID.
        GET /api/v1/payments/{paymentID}
        """
        logger.info('RollyPay get_payment', payment_id=payment_id)

        try:
            session = await self._get_session()
            async with session.get(
                f'{API_BASE_URL}/payments/{payment_id}',
                headers=self._build_headers(),
            ) as response:
                data = await response.json(content_type=None)

                if response.status == 200:
                    return data

                error_msg = data.get('message') or data.get('error') or str(data)
                logger.error(
                    'RollyPay get_payment error',
                    status_code=response.status,
                    error_msg=error_msg,
                )
                raise RollyPayAPIError(response.status, error_msg)

        except aiohttp.ClientError as e:
            logger.exception('RollyPay API connection error', error=e)
            raise

    def verify_webhook_signature(self, raw_body: bytes, received_signature: str, timestamp: str) -> bool:
        """Верификация подписи webhook RollyPay через HMAC-SHA256.

        Signature = HMAC-SHA256(signing_secret, f"{timestamp}.{raw_body_str}")
        Headers: X-Signature, X-Timestamp
        """
        try:
            if not received_signature or not timestamp:
                logger.warning('RollyPay webhook: отсутствует signature или timestamp')
                return False

            raw_body_str = raw_body.decode('utf-8')
            message = f'{timestamp}.{raw_body_str}'

            expected = hmac.new(
                self.signing_secret.encode('utf-8'),
                message.encode('utf-8'),
                hashlib.sha256,
            ).hexdigest()

            return hmac.compare_digest(expected, received_signature)
        except Exception as e:
            logger.error('RollyPay webhook verify error', error=e)
            return False


# Singleton instance
rollypay_service = RollyPayService()
