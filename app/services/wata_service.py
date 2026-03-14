"""High level service for interacting with WATA payment API."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

# WATA API rejects expirationDateTime <= now + 10 minutes (exclusive lower bound).
# 15 minutes provides a 5-minute buffer against clock skew and request latency.
_MIN_EXPIRATION_MINUTES = 15


class WataAPIError(RuntimeError):
    """Raised when the WATA API returns an error response."""


class WataService:
    """Thin wrapper around the WATA REST API used for balance top-ups."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        access_token: str | None = None,
        request_timeout: int | None = None,
    ) -> None:
        self.base_url = (base_url or settings.WATA_BASE_URL or '').rstrip('/')
        self.access_token = access_token or settings.WATA_ACCESS_TOKEN
        self.request_timeout = request_timeout or int(settings.WATA_REQUEST_TIMEOUT)

    @property
    def is_configured(self) -> bool:
        return bool(settings.is_wata_enabled() and self.base_url and self.access_token)

    def _build_url(self, path: str) -> str:
        return f'{self.base_url}/{path.lstrip("/")}'

    def _build_headers(self) -> dict[str, str]:
        if not self.access_token:
            raise WataAPIError('WATA access token is not configured')
        return {
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/json',
        }

    _MAX_RETRIES = 2

    @staticmethod
    def _parse_retry_after(response: aiohttp.ClientResponse, response_text: str) -> float:
        """Extract retry delay from Retry-After header or response body."""
        retry_after = response.headers.get('Retry-After')
        if retry_after:
            try:
                return float(retry_after)
            except (ValueError, TypeError):
                pass

        match = re.search(r'[Rr]etry after (\d+)', response_text)
        if match:
            return float(match.group(1))

        return 45.0

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.is_configured:
            raise WataAPIError('WATA service is not configured')

        url = self._build_url(path)
        timeout = aiohttp.ClientTimeout(total=self.request_timeout)

        last_error: WataAPIError | None = None
        for attempt in range(1 + self._MAX_RETRIES):
            try:
                async with (
                    aiohttp.ClientSession(timeout=timeout) as session,
                    session.request(
                        method,
                        url,
                        json=json,
                        params=params,
                        headers=self._build_headers(),
                    ) as response,
                ):
                    response_text = await response.text()

                    if response.status == 429:
                        retry_delay = self._parse_retry_after(response, response_text)
                        if attempt < self._MAX_RETRIES:
                            logger.warning(
                                'WATA API 429 on , retry / after s',
                                method=method,
                                path=path,
                                attempt=attempt + 1,
                                MAX_RETRIES=self._MAX_RETRIES,
                                retry_delay=retry_delay,
                            )
                            await asyncio.sleep(retry_delay)
                            continue
                        logger.warning('WATA API 429 on , retries exhausted', method=method, path=path)
                        last_error = WataAPIError(f'WATA API rate limited on {method} {path}')
                        break

                    if response.status >= 400:
                        logger.error('WATA API error', response_status=response.status, response_text=response_text)
                        raise WataAPIError(f'WATA API returned status {response.status}: {response_text}')

                    if not response_text:
                        return {}

                    try:
                        data = await response.json()
                    except aiohttp.ContentTypeError as error:
                        logger.error('WATA API returned non-JSON response', error=error)
                        raise WataAPIError('WATA API returned invalid JSON') from error

                    return data
            except aiohttp.ClientError as error:
                logger.error('Error communicating with WATA API', error=error)
                raise WataAPIError('Failed to communicate with WATA API') from error

        raise last_error or WataAPIError('WATA API request failed')

    @staticmethod
    def _amount_from_kopeks(amount_kopeks: int) -> float:
        return round(amount_kopeks / 100, 2)

    @staticmethod
    def _format_datetime(value: datetime) -> str:
        if value.tzinfo is None:
            aware = value.replace(tzinfo=UTC)
        else:
            aware = value.astimezone(UTC)
        return aware.isoformat().replace('+00:00', 'Z')

    @staticmethod
    def _parse_datetime(raw: str | None) -> datetime | None:
        if not raw:
            return None
        try:
            normalized = raw.replace('Z', '+00:00')
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except (ValueError, TypeError):
            logger.debug('Failed to parse WATA datetime', raw=raw)
            return None

    async def create_payment_link(
        self,
        *,
        amount_kopeks: int,
        currency: str,
        description: str,
        order_id: str,
        success_url: str | None = None,
        fail_url: str | None = None,
        link_type: str | None = None,
        expiration_minutes: int | None = None,
        allow_arbitrary_amount: bool = False,
        arbitrary_amount_prompts: list[int] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'amount': self._amount_from_kopeks(amount_kopeks),
            'currency': currency,
            'description': description,
            'orderId': order_id,
        }

        payment_type = link_type or settings.WATA_PAYMENT_TYPE
        if payment_type:
            payload['type'] = payment_type

        if success_url or settings.WATA_SUCCESS_REDIRECT_URL:
            payload['successRedirectUrl'] = success_url or settings.WATA_SUCCESS_REDIRECT_URL
        if fail_url or settings.WATA_FAIL_REDIRECT_URL:
            payload['failRedirectUrl'] = fail_url or settings.WATA_FAIL_REDIRECT_URL

        if expiration_minutes is None:
            ttl = settings.WATA_LINK_TTL_MINUTES
            expiration_minutes = int(ttl) if ttl is not None else None

        if expiration_minutes:
            # WATA API требует expirationDateTime строго > now + 10 минут.
            # Принудительный минимум 15 минут, чтобы не попасть на границу.
            safe_minutes = max(expiration_minutes, _MIN_EXPIRATION_MINUTES)
            expiration_time = datetime.now(UTC) + timedelta(minutes=safe_minutes)
            payload['expirationDateTime'] = self._format_datetime(expiration_time)

        if allow_arbitrary_amount:
            payload['isArbitraryAmountAllowed'] = True
            if arbitrary_amount_prompts:
                payload['arbitraryAmountPrompts'] = arbitrary_amount_prompts

        logger.info(
            'Создаем WATA платежную ссылку: order_id amount',
            order_id=order_id,
            payload=payload['amount'],
            currency=currency,
        )

        response = await self._request('POST', '/links', json=payload)
        logger.debug('WATA create link response', response=response)
        return response

    async def get_payment_link(self, payment_link_id: str) -> dict[str, Any]:
        logger.debug('Запрашиваем WATA ссылку', payment_link_id=payment_link_id)
        return await self._request('GET', f'/links/{payment_link_id}')

    async def search_transactions(
        self,
        *,
        order_id: str | None = None,
        payment_link_id: str | None = None,
        status: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            'skipCount': 0,
            'maxResultCount': max(1, min(limit, 1000)),
        }
        if order_id:
            params['orderId'] = order_id
        if status:
            params['statuses'] = status
        if payment_link_id:
            params['paymentLinkId'] = payment_link_id

        logger.debug(
            'Ищем WATA транзакции: order_id payment_link_id', order_id=order_id, payment_link_id=payment_link_id
        )
        return await self._request('GET', '/transactions', params=params)

    async def get_transaction(self, transaction_id: str) -> dict[str, Any]:
        logger.debug('Получаем WATA транзакцию', transaction_id=transaction_id)
        return await self._request('GET', f'/transactions/{transaction_id}')
