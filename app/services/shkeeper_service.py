"""Клиент для работы с API SHKeeper."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class ShkeeperAPIError(RuntimeError):
    """Ошибка API SHKeeper."""


class ShkeeperService:
    """Обертка над HTTP API SHKeeper для создания инвойсов и проверки статуса."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        callback_api_key: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.base_url = (base_url or settings.SHKEEPER_BASE_URL or '').rstrip('/')
        self.api_key = api_key or settings.SHKEEPER_API_KEY
        self.callback_api_key = callback_api_key or settings.SHKEEPER_CALLBACK_API_KEY or settings.SHKEEPER_API_KEY
        self.timeout = int(timeout or settings.SHKEEPER_REQUEST_TIMEOUT)

    @property
    def is_configured(self) -> bool:
        return bool(settings.is_shkeeper_enabled() and self.base_url and self.api_key)

    def _build_headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ShkeeperAPIError('SHKeeper API ключ не настроен')
        return {
            'accept': 'application/json',
            'content-type': 'application/json',
            'X-Shkeeper-API-Key': self.api_key,
        }

    def verify_callback_auth(self, header_value: str | None) -> bool:
        expected = (self.callback_api_key or '').strip()
        actual = (header_value or '').strip()
        return bool(expected and actual and expected == actual)

    async def _request(self, method: str, path: str, *, json_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.is_configured:
            raise ShkeeperAPIError('SHKeeper не настроен')

        timeout = aiohttp.ClientTimeout(total=self.timeout)
        url = f'{self.base_url}/{path.lstrip("/")}'

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method=method,
                    url=url,
                    headers=self._build_headers(),
                    json=json_payload,
                ) as response:
                    response_text = await response.text()
                    if response.status >= 400:
                        logger.error('SHKeeper API вернул ошибку', status=response.status, body=response_text)
                        raise ShkeeperAPIError(f'SHKeeper API status={response.status}')

                    if not response_text:
                        return {}

                    try:
                        data = await response.json()
                    except aiohttp.ContentTypeError as error:
                        logger.error('SHKeeper API вернул не JSON', body=response_text)
                        raise ShkeeperAPIError('SHKeeper API вернул не JSON') from error

                    return data if isinstance(data, dict) else {}
        except aiohttp.ClientError as error:
            logger.error('Ошибка запроса к SHKeeper API', error=error)
            raise ShkeeperAPIError('Не удалось выполнить запрос к SHKeeper API') from error

    async def create_invoice(
        self,
        *,
        amount_usd: float,
        order_id: str,
        description: str,
        callback_url: str | None = None,
        success_url: str | None = None,
        fail_url: str | None = None,
    ) -> dict[str, Any]:
        crypto = (settings.SHKEEPER_CRYPTO or 'USDT').strip()
        payload: dict[str, Any] = {
            'external_id': order_id,
            'amount': f'{amount_usd:.2f}',
            'description': description,
        }

        if callback_url:
            payload['callback_url'] = callback_url
        if success_url:
            payload['success_url'] = success_url
        if fail_url:
            payload['fail_url'] = fail_url

        logger.info('Создаем инвойс SHKeeper', order_id=order_id, amount_usd=amount_usd, crypto=crypto)
        response = await self._request('POST', f'/api/v1/{crypto}/payment_request', json_payload=payload)
        logger.debug('Ответ SHKeeper create_invoice', response=response)
        return response

    async def get_invoice_status(
        self,
        *,
        invoice_id: str | None = None,
        external_id: str | None = None,
    ) -> dict[str, Any]:
        if external_id:
            return await self._request('GET', f'/api/v1/invoices/{external_id}')
        if invoice_id:
            return await self._request('GET', f'/api/v1/invoices/{invoice_id}')
        raise ShkeeperAPIError('Не передан invoice_id или external_id')

    @staticmethod
    def parse_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            raw = value.replace('Z', '+00:00')
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except Exception:
            return None
