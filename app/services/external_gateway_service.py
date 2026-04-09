"""HTTP-клиент для взаимодействия с внешним платёжным шлюзом."""

from __future__ import annotations

import hmac
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class ExternalGatewayService:
    """Сервис для работы с внешним платёжным шлюзом (paygate API)."""

    def __init__(self) -> None:
        self._base_url = settings.EXTERNAL_GATEWAY_URL.rstrip('/')
        self._api_key = settings.EXTERNAL_GATEWAY_API_KEY
        self._webhook_secret = settings.EXTERNAL_GATEWAY_WEBHOOK_SECRET
        self._timeout = aiohttp.ClientTimeout(total=settings.EXTERNAL_GATEWAY_PAYMENT_TIMEOUT_SECONDS)

    def _headers(self) -> dict[str, str]:
        return {
            'X-Api-Key': self._api_key,
            'Content-Type': 'application/json',
        }

    async def create_payment(
        self,
        *,
        amount: float,
        currency: str,
        order_id: str,
        callback_url: str,
        description: str = 'Пополнение баланса',
        return_url: str | None = None,
        method: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Создаёт платёж через внешний шлюз.

        Returns:
            dict с ключами success, order_id, redirect_url или None при ошибке.
        """
        create_path = settings.EXTERNAL_GATEWAY_CREATE_PATH
        url = f'{self._base_url}{create_path}'

        payload: dict[str, Any] = {
            'amount': amount,
            'currency': currency,
            'order_id': order_id,
            'callback_url': callback_url,
            'product_name': description,
        }
        if return_url:
            payload['return_url'] = return_url
        if method:
            payload['method'] = method
        if metadata:
            payload['metadata'] = metadata

        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(url, json=payload, headers=self._headers()) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(
                            'External gateway create_payment error',
                            status=resp.status,
                            body=body[:500],
                        )
                        return None
                    data = await resp.json()
                    if not data.get('success'):
                        logger.error('External gateway create_payment failed', response=data)
                        return None
                    return data
        except Exception:
            logger.exception('External gateway create_payment exception')
            return None

    async def check_status(self, order_id: str) -> dict[str, Any] | None:
        """Проверяет статус платежа через внешний шлюз.

        Returns:
            dict с ключами success, status, order_id и т.д., или None при ошибке.
        """
        status_path = settings.EXTERNAL_GATEWAY_STATUS_PATH
        url = f'{self._base_url}{status_path}'
        params = {'order_id': order_id}

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.get(url, params=params, headers=self._headers()) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(
                            'External gateway check_status error',
                            status=resp.status,
                            body=body[:500],
                        )
                        return None
                    return await resp.json()
        except Exception:
            logger.exception('External gateway check_status exception')
            return None

    def verify_webhook_secret(self, received_secret: str) -> bool:
        """Проверяет X-Webhook-Secret заголовок из callback."""
        if not self._webhook_secret:
            logger.warning('EXTERNAL_GATEWAY_WEBHOOK_SECRET is not set, rejecting callback')
            return False
        return hmac.compare_digest(received_secret, self._webhook_secret)
