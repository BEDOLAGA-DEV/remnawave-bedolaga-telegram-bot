"""Сервис для работы с API RioPay (api.riopay.online) v2.0.1."""

import hashlib
import hmac
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

API_BASE_URL = 'https://api.riopay.online'


class RioPayAPIError(Exception):
    """Ошибка API RioPay."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f'RioPay API error ({status_code}): {message}')


class RioPayService:
    """Сервис для работы с API RioPay."""

    def __init__(self):
        self._api_token: str | None = None
        self._session: aiohttp.ClientSession | None = None

    @property
    def api_token(self) -> str:
        if self._api_token is None:
            self._api_token = settings.RIOPAY_API_TOKEN
        return self._api_token or ''

    @property
    def webhook_secret(self) -> str:
        """Ключ для HMAC-SHA512 верификации вебхуков. По умолчанию = api_token."""
        return settings.RIOPAY_WEBHOOK_SECRET or self.api_token

    def _get_headers(self) -> dict[str, str]:
        """Формирует заголовки для API запросов."""
        return {
            'X-Api-Token': self.api_token,
            'Content-Type': 'application/json',
        }

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

    async def create_order(
        self,
        *,
        amount: float,
        external_id: str,
        purpose: str = 'Пополнение баланса',
        success_url: str | None = None,
    ) -> dict[str, Any]:
        """
        Создает заказ через API RioPay.
        POST /v1/orders

        Returns:
            OrderData dict с полями id, status, paymentLink, amount, currency, etc.
        """
        payload: dict[str, Any] = {
            'amount': str(amount),
            'externalId': external_id,
            'purpose': purpose,
        }

        if success_url:
            payload['successUrl'] = success_url

        logger.info(
            'RioPay API create_order',
            external_id=external_id,
            amount=amount,
        )

        try:
            session = await self._get_session()
            async with session.post(
                f'{API_BASE_URL}/v1/orders',
                json=payload,
                headers=self._get_headers(),
            ) as response:
                if response.status == 201:
                    data = await response.json(content_type=None)
                    logger.info('RioPay API order created', status_code=response.status, order_id=data.get('id'))
                    return data

                # Ошибка
                text = await response.text()
                try:
                    error_data = await response.json(content_type=None)
                    error_msg = error_data.get('message') or error_data.get('error') or text
                except Exception:
                    error_msg = text

                logger.error('RioPay create_order error', status_code=response.status)
                logger.debug('RioPay create_order error details', error_msg=error_msg)
                raise RioPayAPIError(response.status, error_msg)

        except aiohttp.ClientError as e:
            logger.exception('RioPay API connection error', error=e)
            raise

    async def get_order(self, order_id: str) -> dict[str, Any]:
        """
        Получает заказ по UUID.
        GET /v1/orders/{id}
        """
        logger.info('RioPay get_order', order_id=order_id)

        try:
            session = await self._get_session()
            async with session.get(
                f'{API_BASE_URL}/v1/orders/{order_id}',
                headers=self._get_headers(),
            ) as response:
                if response.status == 200:
                    return await response.json(content_type=None)

                text = await response.text()
                logger.error('RioPay get_order error', status_code=response.status)
                raise RioPayAPIError(response.status, text)

        except aiohttp.ClientError as e:
            logger.exception('RioPay API connection error', error=e)
            raise

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        """HMAC-SHA512 верификация подписи webhook."""
        try:
            expected = hmac.new(
                self.webhook_secret.encode(),
                raw_body,
                hashlib.sha512,
            ).hexdigest()
            return hmac.compare_digest(expected, signature)
        except Exception as e:
            logger.error('RioPay webhook verify error', error=e)
            return False


# Singleton instance
riopay_service = RioPayService()
