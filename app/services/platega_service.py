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


class PlategaApiError(RuntimeError):
    """Platega ответила HTTP-ошибкой с телом (не транспортный сбой).

    Наследует RuntimeError, чтобы существующие ``except RuntimeError``
    у вызывающих продолжали работать; ``str(error)`` несёт человекочитаемую
    причину провайдера для actionable-ответа UI/админу.
    """

    def __init__(self, http_status: int, message: str) -> None:
        super().__init__(message)
        self.http_status = http_status


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

    async def create_subscription(
        self,
        *,
        amount: float,
        currency: str,
        interval: int,
        description: str | None = None,
    ) -> dict[str, Any] | None:
        body: dict[str, Any] = {
            'paymentMethod': 6,
            'paymentDetails': {
                'amount': self._format_amount(amount),
                'currency': currency,
                'interval': interval,
            },
        }

        if description:
            body['description'] = self._sanitize_description(description, self._description_max_length)

        # Тот же выбор версии эндпоинта, что и в create_payment (см. #2934):
        # v1 POST /transaction/process, v2 POST /v2/transaction/process.
        endpoint = '/v2/transaction/process' if self.api_version == 'v2' else '/transaction/process'
        data, http_status = await self._request('POST', endpoint, json_data=body, return_status=True)

        if http_status is not None and http_status >= 400:
            # Пробрасываем причину провайдера наверх: голое None превращалось в
            # безликий 409 в кабинете, а реальная причина жила только в логах.
            raise PlategaApiError(http_status, self._describe_subscription_error(data))

        return data

    @staticmethod
    def _describe_subscription_error(data: Any) -> str:
        """Человекочитаемая причина отказа Platega на создании подписки."""
        detail = ''
        payment_method_rejected = False
        if isinstance(data, dict):
            items = data.get('data')
            if isinstance(items, list):
                parts = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    parts.append(f'{item.get("key")}: {item.get("message")}')
                    if item.get('key') == 'paymentMethod':
                        payment_method_rejected = True
                detail = '; '.join(parts)
            detail = detail or str(data.get('message') or '')

        message = f'Platega отклонила создание подписки ({detail or "без деталей"})'
        if payment_method_rejected:
            # VAL_0001 с key=paymentMethod на методе 6: формат запроса совпадает
            # с документацией — так Platega отвечает, когда метод Subscription
            # не включён для мерчанта (ср. карточные каскады в #2934).
            message += '. Похоже, метод Subscription не включён для вашего мерчанта — запросите включение рекуррентных платежей в поддержке Platega'
        return message

    async def get_subscription(self, subscription_id: str) -> dict[str, Any] | None:
        # Как и статусный GET транзакции, эндпоинт подписки не версионируется.
        endpoint = f'/subscription/{subscription_id}'
        return await self._request('GET', endpoint)

    async def get_subscription_status(self, subscription_id: str) -> tuple[dict[str, Any] | None, int | None]:
        """GET подписки с HTTP-статусом: (payload, status).

        Нужен reconciler'у, чтобы отличать «подписки нет у провайдера»
        (HTTP 404 → можно хоронить локальную запись) от «Platega недоступна»
        (status=None → решение откладывается до следующего цикла).
        """
        endpoint = f'/subscription/{subscription_id}'
        return await self._request('GET', endpoint, return_status=True)

    async def list_subscriptions(
        self,
        *,
        status: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page: int | None = None,
        size: int | None = None,
    ) -> dict[str, Any] | None:
        params: dict[str, Any] = {}
        if status is not None:
            params['status'] = status
        if date_from is not None:
            params['from'] = date_from
        if date_to is not None:
            params['to'] = date_to
        if page is not None:
            params['page'] = page
        if size is not None:
            params['size'] = size

        return await self._request('GET', '/subscription', params=params)

    async def cancel_subscription(self, subscription_id: str) -> dict[str, Any] | None:
        # Неверсионированный эндпоинт, аналогично get_subscription/get_transaction.
        endpoint = f'/subscription/{subscription_id}/cancel'
        return await self._request('POST', endpoint)

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        return_status: bool = False,
    ) -> Any:
        """HTTP-запрос к Platega.

        По умолчанию возвращает payload или None (любой сбой). С
        ``return_status=True`` — кортеж ``(payload, http_status | None)``:
        status=None означает транспортный сбой (таймаут/сеть/неконфигурация),
        а не ответ провайдера — вызывающий может отличить «404: объекта нет»
        от «Platega недоступна».
        """

        def _result(data: dict[str, Any] | None, status: int | None) -> Any:
            return (data, status) if return_status else data

        if not self.is_configured:
            logger.error('Platega service is not configured')
            return _result(None, None)

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
                        # В status-режиме отдаём и тело ошибки — вызывающий
                        # может показать причину провайдера (VAL_* детали).
                        return _result(data if return_status else None, response.status)

                    return _result(data, response.status)
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
                return _result(None, None)

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

        return _result(None, None)

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
    def _format_amount(amount: float) -> int | float:
        """Platega ждёт целое число для суммы без копеек и float — иначе (SBP-подписки)."""

        return int(amount) if amount == int(amount) else round(amount, 2)

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
