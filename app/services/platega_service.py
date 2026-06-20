"""HTTP-интеграция с Platega API."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class PlategaService:
    """Обертка над Platega API с базовой повторной отправкой запросов."""

    def __init__(self) -> None:
        self.base_url = (settings.PLATEGA_BASE_URL or 'https://app.platega.io').rstrip('/')
        self.merchant_id = settings.PLATEGA_MERCHANT_ID
        self.secret = settings.PLATEGA_SECRET
        self._timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=25)
        self._max_retries = 3
        self._retry_delay = 0.5
        self._retryable_statuses = {500, 502, 503, 504}
        self._description_max_length = 64

    @property
    def is_configured(self) -> bool:
        return settings.is_platega_enabled()

    async def create_payment(
        self,
        *,
        payment_method: int,
        amount: float,
        currency: str,
        description: str | None = None,
        return_url: str | None = None,
        failed_url: str | None = None,
        payload: str | None = None,
    ) -> dict[str, Any] | None:
        body: dict[str, Any] = {
            'paymentMethod': payment_method,
            'paymentDetails': {
                'amount': round(amount, 2),
                'currency': currency,
            },
        }

        if description:
            sanitized_description = self._sanitize_description(description, self._description_max_length)
            body['description'] = sanitized_description
        if return_url:
            body['return'] = return_url
        if failed_url:
            body['failedUrl'] = failed_url
        if payload:
            body['payload'] = payload

        return await self._request('POST', '/transaction/process', json_data=body)

    async def get_transaction(self, transaction_id: str) -> dict[str, Any] | None:
        endpoint = f'/transaction/{transaction_id}'
        return await self._request('GET', endpoint)

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not self.is_configured:
            logger.error('Platega service is not configured')
            return None

        url = f'{self.base_url}{endpoint}'
        headers = {
            'X-MerchantId': self.merchant_id or '',
            'X-Secret': self.secret or '',
            'Content-Type': 'application/json',
        }

        last_error: BaseException | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                async with (
                    aiohttp.ClientSession(timeout=self._timeout) as session,
                    session.request(
                        method,
                        url,
                        json=json_data,
                        params=params,
                        headers=headers,
                    ) as response,
                ):
                    data, raw_text = await self._deserialize_response(response)

                    if response.status >= 400:
                        logger.error(
                            'Platega API error', response_status=response.status, endpoint=endpoint, raw_text=raw_text
                        )
                        if response.status in self._retryable_statuses and attempt < self._max_retries:
                            await asyncio.sleep(self._retry_delay * attempt)
                            continue
                        return None

                    return data
            except asyncio.CancelledError:
                logger.debug('Platega request cancelled', method=method, endpoint=endpoint)
                raise
            except TimeoutError as error:
                last_error = error
                logger.warning(
                    'Platega request timeout, retrying',
                    method=method,
                    endpoint=endpoint,
                    attempt=attempt,
                    max_retries=self._max_retries,
                )
            except aiohttp.ClientError as error:
                last_error = error
                logger.warning(
                    'Platega client error, retrying',
                    method=method,
                    endpoint=endpoint,
                    attempt=attempt,
                    max_retries=self._max_retries,
                    error=error,
                )
            except Exception as error:  # pragma: no cover - safety
                logger.exception('Unexpected Platega error', error=error)
                return None

            if attempt < self._max_retries:
                await asyncio.sleep(self._retry_delay * attempt)

        if last_error is not None:
            logger.error(
                'Platega request failed after all retries',
                max_retries=self._max_retries,
                method=method,
                endpoint=endpoint,
                last_error=last_error,
            )

        return None

    @staticmethod
    async def _deserialize_response(
        response: aiohttp.ClientResponse,
    ) -> tuple[dict[str, Any] | None, str]:
        raw_text = await response.text()
        if not raw_text:
            return None, ''

        content_type = response.headers.get('Content-Type', '')
        if 'json' in content_type.lower() or not content_type:
            try:
                return json.loads(raw_text), raw_text
            except json.JSONDecodeError as error:
                logger.error('Failed to decode Platega JSON response', url=response.url, error=error)
                return None, raw_text

        return None, raw_text

    @staticmethod
    def _sanitize_description(description: str, max_bytes: int) -> str:
        """Обрезает описание с учётом байтового лимита Platega."""

        cleaned = (description or '').strip()
        if not max_bytes:
            return cleaned

        encoded = cleaned.encode('utf-8')
        if len(encoded) <= max_bytes:
            return cleaned

        logger.debug('Platega description trimmed from to bytes', encoded_count=len(encoded), max_bytes=max_bytes)

        trimmed_bytes = encoded[:max_bytes]
        while True:
            try:
                return trimmed_bytes.decode('utf-8')
            except UnicodeDecodeError:
                trimmed_bytes = trimmed_bytes[:-1]

    @staticmethod
    def parse_expires_at(expires_in: str | None) -> datetime | None:
        if not expires_in:
            return None

        try:
            hours, minutes, seconds = [int(part) for part in expires_in.split(':', 2)]
            delta = timedelta(hours=hours, minutes=minutes, seconds=seconds)
            return datetime.now(UTC) + delta
        except Exception:
            logger.warning('Failed to parse Platega expiresIn value', expires_in=expires_in)
            return None
