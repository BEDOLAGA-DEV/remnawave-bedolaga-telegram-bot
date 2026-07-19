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

    _SUPPORTED_API_VERSIONS = ('v1', 'v2')

    def __init__(self) -> None:
        base_url = (settings.PLATEGA_BASE_URL or 'https://app.platega.io').rstrip('/')
        # Совместимость с обходом из #2934: версию дописывали прямо в
        # PLATEGA_BASE_URL (…/v2). Суффикс срезаем и трактуем как форс версии,
        # иначе create собрал бы путь /v2/v2/transaction/process, а статусный
        # GET (неверсионированный по докам Platega) уезжал бы на /v2/transaction/{id}.
        forced_version: str | None = None
        for candidate in self._SUPPORTED_API_VERSIONS:
            suffix = f'/{candidate}'
            # Case-insensitive: a manually appended suffix may be '/V2', which would
            # otherwise slip through and build a malformed '/V2/transaction/process'.
            if base_url.lower().endswith(suffix):
                forced_version = candidate
                base_url = base_url[: -len(suffix)].rstrip('/')
                logger.info(
                    'PLATEGA_BASE_URL содержит суффикс версии — вынесен в версию API',
                    api_version=candidate,
                    base_url=base_url,
                )
                break
        self.base_url = base_url
        self.api_version = forced_version or self._normalize_api_version(settings.PLATEGA_API_VERSION)
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

        # v1 POST /transaction/process — документированный flow с заданным
        # paymentMethod (ссылка в поле `redirect`). v2 POST /v2/transaction/process
        # отвечает полем `url` и нужен мерчантам, у которых карточные каскады
        # работают только в v2 (#2934: v1 отдаёт 400 «No available card cascades»).
        endpoint = '/v2/transaction/process' if self.api_version == 'v2' else '/transaction/process'
        return await self._request('POST', endpoint, json_data=body)

    async def get_transaction(self, transaction_id: str) -> dict[str, Any] | None:
        # Статусный GET не версионируется: в доках Platega путь один — /transaction/{id}.
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

    @classmethod
    def _normalize_api_version(cls, raw: str | None) -> str:
        version = (raw or '').strip().lower()
        if version in cls._SUPPORTED_API_VERSIONS:
            return version
        if version:
            logger.warning(
                'Неизвестное значение PLATEGA_API_VERSION, используется v1',
                configured=raw,
                supported=cls._SUPPORTED_API_VERSIONS,
            )
        return 'v1'

    @staticmethod
    def parse_redirect_url(response: dict[str, Any] | None) -> str | None:
        """Ссылка на страницу оплаты из ответа create: v1 отдаёт `redirect`, v2 — `url`.

        Принимаем оба поля независимо от настроенной версии — ответ парсится
        одинаково и для чужого PLATEGA_BASE_URL, уже указывающего на v2 (#2934).
        """
        if not response:
            return None
        redirect_url = response.get('redirect') or response.get('url')
        return str(redirect_url) if redirect_url else None

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
