"""Async client for PayPalych (Pal24) API."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class Pal24APIError(Exception):
    """Base error for Pal24 API operations."""


@dataclass(slots=True)
class Pal24Response:
    """Wrapper for Pal24 API responses."""

    success: bool
    data: dict[str, Any]
    status: int

    @classmethod
    def from_payload(cls, payload: dict[str, Any], status: int) -> Pal24Response:
        success = bool(payload.get('success', status < 400))
        return cls(success=success, data=payload, status=status)

    def raise_for_status(self, endpoint: str) -> None:
        if not self.success:
            detail = self.data.get('message') or self.data.get('error')
            raise Pal24APIError(f'Pal24 API error at {endpoint}: status={self.status}, detail={detail or self.data}')


class Pal24Client:
    """Async client implementing PayPalych API methods."""

    def __init__(
        self,
        *,
        api_token: str | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.api_token = api_token or settings.PAL24_API_TOKEN
        self.base_url = (base_url or settings.PAL24_BASE_URL or '').rstrip('/') + '/'
        self.timeout = timeout or settings.PAL24_REQUEST_TIMEOUT

        if not self.api_token:
            logger.warning('Pal24Client initialized without API token')

    @property
    def is_configured(self) -> bool:
        return bool(self.api_token and self.base_url)

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json_payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Pal24Response:
        if not self.is_configured:
            raise Pal24APIError('Pal24 client is not configured')

        url = f'{self.base_url}{endpoint.lstrip("/")}'
        headers = {
            'Authorization': f'Bearer {self.api_token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

        timeout = aiohttp.ClientTimeout(total=self.timeout)

        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.request(
                    method,
                    url,
                    headers=headers,
                    json=json_payload,
                    params=params,
                ) as response,
            ):
                status = response.status
                try:
                    payload = await response.json(content_type=None)
                except aiohttp.ContentTypeError:
                    text_body = await response.text()
                    logger.error('Pal24 API returned non-JSON response for', endpoint=endpoint, text_body=text_body)
                    raise Pal24APIError(f'Pal24 API returned non-JSON response: {text_body}') from None

                result = Pal24Response.from_payload(payload, status)
                if status >= 400 or not result.success:
                    logger.error('Pal24 API error', status=status, endpoint=endpoint, payload=payload)
                    result.raise_for_status(endpoint)

                return result

        except TimeoutError as error:
            logger.error('Pal24 API request timeout for', endpoint=endpoint, error=error)
            raise Pal24APIError(f'Pal24 API request timeout for {endpoint}') from error
        except aiohttp.ClientError as error:
            logger.error('Pal24 API client error for', endpoint=endpoint, error=error)
            raise Pal24APIError(str(error)) from error

    # API methods -----------------------------------------------------------------

    async def create_bill(
        self,
        *,
        amount: Decimal,
        shop_id: str,
        order_id: str | None = None,
        description: str | None = None,
        currency_in: str = 'RUB',
        type_: str = 'normal',
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'amount': str(amount),
            'shop_id': shop_id,
            'currency_in': currency_in,
            'type': type_,
        }

        if order_id:
            payload['order_id'] = order_id
        if description:
            payload['description'] = description

        payload.update({k: v for k, v in kwargs.items() if v is not None})

        response = await self._request('POST', 'bill/create', json_payload=payload)
        return response.data

    async def get_bill_status(self, bill_id: str) -> dict[str, Any]:
        response = await self._request('GET', 'bill/status', params={'id': bill_id})
        return response.data

    async def toggle_bill_activity(self, bill_id: str, active: bool) -> dict[str, Any]:
        payload = {'id': bill_id, 'active': 1 if active else 0}
        response = await self._request('POST', 'bill/toggle_activity', json_payload=payload)
        return response.data

    async def search_payments(self, **params: Any) -> dict[str, Any]:
        response = await self._request('GET', 'payment/search', params=params)
        return response.data

    async def get_payment_status(self, payment_id: str) -> dict[str, Any]:
        response = await self._request('GET', 'payment/status', params={'id': payment_id})
        return response.data

    async def get_balance(self) -> dict[str, Any]:
        response = await self._request('GET', 'merchant/balance')
        return response.data

    async def search_bills(self, **params: Any) -> dict[str, Any]:
        response = await self._request('GET', 'bill/search', params=params)
        return response.data

    async def get_bill_payments(self, bill_id: str) -> dict[str, Any]:
        response = await self._request('GET', 'bill/payments', params={'id': bill_id})
        return response.data

    # Helpers ---------------------------------------------------------------------

    @staticmethod
    def calculate_signature(out_sum: str, inv_id: str, api_token: str | None = None) -> str:
        token = api_token or settings.PAL24_SIGNATURE_TOKEN or settings.PAL24_API_TOKEN
        if not token:
            raise Pal24APIError('Pal24 signature token is not configured')
        raw = f'{out_sum}:{inv_id}:{token}'.encode()
        return hashlib.md5(raw).hexdigest().upper()

    @staticmethod
    def verify_signature(
        out_sum: str,
        inv_id: str,
        signature: str,
        api_token: str | None = None,
    ) -> bool:
        try:
            expected = Pal24Client.calculate_signature(out_sum, inv_id, api_token)
        except Pal24APIError:
            logger.error('Pal24 signature verification failed: missing token')
            return False
        return hmac.compare_digest(expected, signature.upper())

    @staticmethod
    def normalize_amount(amount_kopeks: int) -> Decimal:
        try:
            return (Decimal(amount_kopeks) / Decimal(100)).quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError) as error:
            raise Pal24APIError(f'Invalid amount: {amount_kopeks}') from error
