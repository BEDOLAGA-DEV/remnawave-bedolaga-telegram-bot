"""Сервис для работы с API RioPay (api.riopay.online) v2.0.1."""

import hashlib
import hmac
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

API_BASE_URL = 'https://api.riopay.online'


class RioPayService:
    """Сервис для работы с API RioPay."""

    def __init__(self):
        self._api_token: str | None = None

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
            'x-api-token': self.api_token,
            'Content-Type': 'application/json',
        }

    async def create_order(
        self,
        *,
        amount: float,
        currency: str = 'RUB',
        external_id: str,
        purpose: str = 'Пополнение баланса',
        success_url: str | None = None,
        fail_url: str | None = None,
    ) -> dict[str, Any]:
        """
        Создает заказ через API RioPay.
        POST /v1/orders

        Returns:
            OrderData dict с полями id, status, paymentLink, amount, currency, etc.
        """
        payload: dict[str, Any] = {
            'amount': str(amount),
            'currency': currency,
            'externalId': external_id,
            'purpose': purpose,
        }

        if success_url:
            payload['successUrl'] = success_url
        if fail_url:
            payload['failUrl'] = fail_url

        logger.info(
            'RioPay API create_order',
            external_id=external_id,
            amount=amount,
            currency=currency,
        )

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    f'{API_BASE_URL}/v1/orders',
                    json=payload,
                    headers=self._get_headers(),
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response,
            ):
                text = await response.text()
                logger.info('RioPay API response', status_code=response.status, text=text)

                if response.status == 201:
                    data = await response.json(content_type=None)
                    return data

                # Ошибка
                try:
                    error_data = await response.json(content_type=None)
                    error_msg = error_data.get('message') or error_data.get('error') or text
                except Exception:
                    error_msg = text

                logger.error('RioPay create_order error', status_code=response.status, error_msg=error_msg)
                raise Exception(f'RioPay API error ({response.status}): {error_msg}')

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
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    f'{API_BASE_URL}/v1/orders/{order_id}',
                    headers=self._get_headers(),
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response,
            ):
                text = await response.text()
                logger.info('RioPay get_order response', status_code=response.status, text=text)

                if response.status == 200:
                    return await response.json(content_type=None)

                raise Exception(f'RioPay get_order error ({response.status}): {text}')

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
