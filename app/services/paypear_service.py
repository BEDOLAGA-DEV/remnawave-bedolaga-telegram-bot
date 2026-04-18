"""Сервис для работы с API PayPear (paypear.ru)."""

import hashlib
import hmac
import uuid
from base64 import b64encode
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

API_BASE_URL = 'https://api.paypear.ru/v1'


class PayPearAPIError(Exception):
    """Ошибка API PayPear."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f'PayPear API error ({status_code}): {message}')


class PayPearService:
    """Сервис для работы с API PayPear."""

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    @property
    def shop_id(self) -> str:
        return settings.PAYPEAR_SHOP_ID or ''

    @property
    def secret_key(self) -> str:
        return settings.PAYPEAR_SECRET_KEY or ''

    def _basic_auth_header(self) -> str:
        """Генерирует HTTP Basic Auth заголовок."""
        credentials = f'{self.shop_id}:{self.secret_key}'
        encoded = b64encode(credentials.encode('utf-8')).decode('utf-8')
        return f'Basic {encoded}'

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

    async def create_payment(
        self,
        *,
        order_id: str,
        amount_rubles: float,
        currency: str = 'RUB',
        payment_method_type: str = 'sbp',
        description: str = '',
        return_url: str | None = None,
        webhook_url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Создает платеж через API PayPear.
        POST /v1/payment/
        """
        idempotence_key = str(uuid.uuid4())

        payload: dict[str, Any] = {
            'amount': {
                'value': f'{amount_rubles:.2f}',
                'currency': currency,
            },
            'order_id': order_id,
            'payment_method_data': {
                'type': payment_method_type,
            },
        }

        if description:
            payload['description'] = description
        if return_url:
            payload['confirmation'] = {
                'type': 'redirect',
                'return_url': return_url,
            }
        if webhook_url:
            payload['webhook_url'] = webhook_url
        if metadata:
            payload['metadata'] = metadata

        logger.info(
            'PayPear API create_payment',
            order_id=order_id,
            amount_rubles=amount_rubles,
            currency=currency,
            payment_method_type=payment_method_type,
        )

        try:
            session = await self._get_session()
            async with session.post(
                f'{API_BASE_URL}/payment/',
                json=payload,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': self._basic_auth_header(),
                    'Idempotence-Key': idempotence_key,
                },
            ) as response:
                data = await response.json(content_type=None)

                if response.status == 200 and data.get('success') is True:
                    result = data.get('result', {})
                    logger.info(
                        'PayPear API payment created',
                        order_id=order_id,
                        paypear_id=result.get('id'),
                    )
                    return result

                error_msg = data.get('message') or data.get('error') or str(data)
                logger.error(
                    'PayPear create_payment error',
                    status_code=response.status,
                    error_msg=error_msg,
                    response_data=data,
                )
                raise PayPearAPIError(response.status, error_msg)

        except aiohttp.ClientError as e:
            logger.exception('PayPear API connection error', error=e)
            raise

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        """
        Получает информацию о платеже по ID.
        GET /v1/payment/{id}/
        """
        logger.info('PayPear get_payment', payment_id=payment_id)

        try:
            session = await self._get_session()
            async with session.get(
                f'{API_BASE_URL}/payment/{payment_id}/',
                headers={
                    'Authorization': self._basic_auth_header(),
                },
            ) as response:
                data = await response.json(content_type=None)

                if response.status == 200 and data.get('success') is True:
                    return data.get('result', {})

                error_msg = data.get('message') or data.get('error') or str(data)
                logger.error(
                    'PayPear get_payment error',
                    status_code=response.status,
                    error_msg=error_msg,
                )
                raise PayPearAPIError(response.status, error_msg)

        except aiohttp.ClientError as e:
            logger.exception('PayPear API connection error', error=e)
            raise

    async def get_payment_by_order_id(self, order_id: str) -> dict[str, Any]:
        """
        Получает информацию о платеже по order_id.
        GET /v1/payment/order/{order_id}/
        """
        logger.info('PayPear get_payment_by_order_id', order_id=order_id)

        try:
            session = await self._get_session()
            async with session.get(
                f'{API_BASE_URL}/payment/order/{order_id}/',
                headers={
                    'Authorization': self._basic_auth_header(),
                },
            ) as response:
                data = await response.json(content_type=None)

                if response.status == 200 and data.get('success') is True:
                    return data.get('result', {})

                error_msg = data.get('message') or data.get('error') or str(data)
                logger.error(
                    'PayPear get_payment_by_order_id error',
                    status_code=response.status,
                    error_msg=error_msg,
                )
                raise PayPearAPIError(response.status, error_msg)

        except aiohttp.ClientError as e:
            logger.exception('PayPear API connection error', error=e)
            raise

    def verify_webhook_signature(self, raw_body: bytes, received_signature: str) -> bool:
        """Верификация подписи webhook PayPear через HMAC-SHA256.

        PayPear sends signature in the webhook JSON field 'signature'.
        The signature is HMAC-SHA256(secret_key, raw_body).
        """
        try:
            if not received_signature:
                logger.warning('PayPear webhook: отсутствует signature')
                return False

            expected = hmac.new(
                self.secret_key.encode('utf-8'),
                raw_body,
                hashlib.sha256,
            ).hexdigest()

            return hmac.compare_digest(expected, received_signature)
        except Exception as e:
            logger.error('PayPear webhook verify error', error=e)
            return False


# Singleton instance
paypear_service = PayPearService()
