"""CisPay hosted Merchant API; local balances and outgoing invoices use RUB."""

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

from app.config import settings


@dataclass(frozen=True, slots=True)
class CisPayConfig:
    base_url: str = 'https://api.cispay.app'
    shop_id: str = ''
    api_key: str = field(default='', repr=False)
    timeout: float = 30


class CisPayAPIError(Exception):
    """Safe diagnostic: never expose provider bodies, credentials or checkout URLs."""

    def __init__(self, status_code: int | None = None, message: str = '', *, retry_after: int | None = None):
        self.status_code = status_code
        self.retry_after = retry_after
        self.message = f'cisPay request failed (HTTP {status_code})' if status_code else 'cisPay response unavailable'
        super().__init__(self.message)


def _retry_after(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            return max(0, math.ceil((deadline - datetime.now(UTC)).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return None


def _reject_constant(_value):
    raise ValueError('Invalid JSON number')


class CisPayService:
    """Stateless HTTP transport with one credentials snapshot per operation."""

    @staticmethod
    def snapshot() -> CisPayConfig:
        return CisPayConfig(
            base_url=(settings.CISPAY_BASE_URL or 'https://api.cispay.app').rstrip('/'),
            shop_id=settings.CISPAY_SHOP_ID or '',
            api_key=settings.CISPAY_API_KEY or '',
        )

    @property
    def base_url(self) -> str:
        return self.snapshot().base_url

    @property
    def shop_id(self) -> str:
        return self.snapshot().shop_id

    @property
    def api_key(self) -> str:
        return self.snapshot().api_key

    async def close(self) -> None:
        """Compatibility hook: each request owns and closes its session."""

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        config: CisPayConfig | None = None,
    ) -> dict[str, Any]:
        config = config or self.snapshot()
        raw = (
            json.dumps(json_payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
            if json_payload is not None
            else None
        )
        headers = {'X-Shop-ID': config.shop_id, 'X-Api-Key': config.api_key, 'Content-Type': 'application/json'}
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=config.timeout), cookie_jar=aiohttp.DummyCookieJar()
            ) as session:
                async with session.request(
                    method,
                    f'{config.base_url.rstrip("/")}/{path.lstrip("/")}',
                    data=raw,
                    params=params,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    if response.status not in (200, 201):
                        raise CisPayAPIError(
                            response.status, retry_after=_retry_after(response.headers.get('Retry-After'))
                        )
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        body.extend(chunk)
                        if len(body) > 1_048_576:
                            raise CisPayAPIError(response.status)
                    try:
                        # Preserve fractions of a kopek until the money validator rejects them.
                        data = json.loads(body, parse_float=str, parse_constant=_reject_constant)
                    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
                        raise CisPayAPIError(response.status) from exc
                    if not isinstance(data, dict):
                        raise CisPayAPIError(response.status)
                    return data
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise CisPayAPIError from exc

    async def create_payment(
        self,
        *,
        amount_kopeks: int,
        order_id: str,
        payment_method: str | None = None,
        customer_id: str | None = None,
        description: str | None = None,
        redirect_success_url: str | None = None,
        redirect_fail_url: str | None = None,
        payload: str | None = None,
        config: CisPayConfig | None = None,
    ) -> dict[str, Any]:
        """Create once; a duplicate order_id is rejected by CisPay, not idempotently retried."""
        if type(amount_kopeks) is not int or not 0 < amount_kopeks <= 2_147_483_647:
            raise ValueError('Invalid CisPay minor-unit amount')
        if payment_method not in (None, 'CARD', 'SBP', 'CRYPTO'):
            raise ValueError('Invalid CisPay method')
        if payment_method == 'CARD' and amount_kopeks < 5000:
            raise ValueError('CisPay CARD minimum is 5000 kopeks')
        if not isinstance(order_id, str) or not 1 <= len(order_id) <= 255:
            raise ValueError('Invalid CisPay order ID')
        if payment_method in (None, 'SBP') and (not isinstance(customer_id, str) or not customer_id.strip()):
            raise ValueError('CisPay requires customer_id')
        if customer_id is not None and (not isinstance(customer_id, str) or len(customer_id) > 255):
            raise ValueError('Invalid CisPay customer_id')
        if payload is not None and (not isinstance(payload, str) or len(payload) > 1024):
            raise ValueError('Invalid CisPay payload')
        data: dict[str, Any] = {'amount': amount_kopeks, 'currency': 'RUB', 'order_id': order_id}
        if payment_method is not None:
            data['payment_method'] = payment_method
        if customer_id:
            data['customer_id'] = customer_id
        for key, value, limit in [
            ('description', description, 512),
            ('redirect_success_url', redirect_success_url, 1024),
            ('redirect_fail_url', redirect_fail_url, 1024),
        ]:
            if value:
                if len(value) > limit:
                    raise ValueError(f'CisPay {key} too long')
                data[key] = value
        if payload is not None:
            data['payload'] = payload
        result = await self._request('POST', '/payments', json_payload=data, config=config)
        if not isinstance(result.get('id'), str) or not result['id']:
            raise CisPayAPIError(201)
        return result

    async def check_payment(
        self, *, payment_id: str | None = None, order_id: str | None = None, config: CisPayConfig | None = None
    ) -> dict[str, Any]:
        if payment_id:
            params = {'id': payment_id}
        elif order_id:
            params = {'order_id': order_id}
        else:
            raise ValueError('CisPay requires payment_id or order_id')
        return await self._request('GET', '/payments/status', params=params, config=config)

    async def get_store_capabilities(self, *, config: CisPayConfig | None = None) -> dict[str, Any]:
        return await self._request('GET', '/store/capabilities', config=config)

    async def get_balance(self, *, config: CisPayConfig | None = None) -> dict[str, Any]:
        return await self._request('GET', '/balance', config=config)

    def verify_webhook_signature(
        self, raw_body: bytes, signature: str | None, *, config: CisPayConfig | None = None
    ) -> bool:
        config = config or self.snapshot()
        if not config.api_key or not isinstance(signature, str) or not re.fullmatch('[0-9a-fA-F]{64}', signature):
            return False
        expected = hmac.new(config.api_key.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature.lower())


cispay_service = CisPayService()
