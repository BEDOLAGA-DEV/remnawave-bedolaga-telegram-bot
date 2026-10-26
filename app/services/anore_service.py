"""Anore hosted-checkout HTTP contract; money is represented locally in kopeks."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

from app.config import settings


ANORE_METHODS = ('sbp', 'card', 'yoomoney', 'crypto')
_MAX_RESPONSE_BYTES = 1_048_576


class AnoreAPIError(Exception):
    """Safe error: never includes credentials, payment links or the provider body."""

    def __init__(self, status_code: int | None = None, *, retry_after: int | None = None) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(f'Anore request failed (HTTP {status_code})' if status_code else 'Anore response unavailable')


@dataclass(frozen=True, slots=True)
class AnoreConfig:
    """One immutable snapshot per operation, including sandbox identity."""

    api_url: str = 'https://api.anore.cc/v1'
    api_key: str = field(default='', repr=False)
    secret_key: str = field(default='', repr=False)
    shop_id: int | None = None
    timeout: float = 30
    methods: tuple[str, ...] = ANORE_METHODS
    min_amount_kopeks: int = 10000
    max_amount_kopeks: int = 100000000

    @property
    def is_test(self) -> bool:
        return self.api_key.startswith('an_test_')


def parse_rubles_to_kopeks(value: object) -> int:
    """Reject non-money, non-finite values and fractions of a kopek; never round."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('Invalid Anore amount')
    try:
        rubles = Decimal(str(value))
        if not rubles.is_finite() or rubles <= 0 or rubles > 100_000_000_000:
            raise ValueError('Invalid Anore amount')
        kopeks = rubles * 100
        if kopeks != kopeks.to_integral_value():
            raise ValueError('Fractional kopek')
        return int(kopeks)
    except (InvalidOperation, OverflowError) as exc:
        raise ValueError('Invalid Anore amount') from exc


def _reject_constant(_value: str) -> None:
    raise ValueError('Non-finite JSON number')


def _retry_after(value: str | None) -> int | None:
    if not value:
        return None
    try:
        seconds = int(value)
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            seconds = math.ceil((deadline - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, seconds)


class AnoreService:
    """Stateless async transport; no automatic POST retries or shared cookie jar."""

    @staticmethod
    def snapshot() -> AnoreConfig:
        return AnoreConfig(
            api_url=settings.ANORE_API_URL.rstrip('/'),
            api_key=settings.ANORE_API_KEY or '',
            secret_key=settings.ANORE_SECRET_KEY or '',
            shop_id=settings.ANORE_SHOP_ID,
            timeout=settings.ANORE_REQUEST_TIMEOUT,
            methods=tuple(settings.get_anore_methods()),
            min_amount_kopeks=settings.ANORE_MIN_AMOUNT_KOPEKS,
            max_amount_kopeks=settings.ANORE_MAX_AMOUNT_KOPEKS,
        )

    @staticmethod
    def verify_callback_signature(raw_body: bytes, signature: str, *, secret: str | None = None) -> bool:
        secret = settings.ANORE_SECRET_KEY if secret is None else secret
        if not secret or not isinstance(signature, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', signature):
            return False
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature.lower())

    async def _request(
        self, method: str, path: str, *, config: AnoreConfig, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not config.api_key:
            raise AnoreAPIError
        raw = (
            json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
            if payload
            else None
        )
        headers = {'Authorization': f'Bearer {config.api_key}', 'Content-Type': 'application/json'}
        if raw is not None and config.secret_key:
            headers['Anore-Signature'] = hmac.new(config.secret_key.encode(), raw, hashlib.sha256).hexdigest()
        try:
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=config.timeout), cookie_jar=aiohttp.DummyCookieJar()
                ) as session,
                session.request(
                    method, f'{config.api_url.rstrip("/")}{path}', headers=headers, data=raw, allow_redirects=False
                ) as response,
            ):
                if response.status != 200:
                    raise AnoreAPIError(response.status, retry_after=_retry_after(response.headers.get('Retry-After')))
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        raise AnoreAPIError(response.status)
                try:
                    data = json.loads(body, parse_float=str, parse_constant=_reject_constant)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise AnoreAPIError(response.status) from exc
                if not isinstance(data, dict) or data.get('success') is not True:
                    raise AnoreAPIError(response.status)
                return data
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise AnoreAPIError from exc

    async def create_payment(
        self,
        *,
        amount_kopeks: int,
        description: str,
        order_id: str,
        methods: list[str] | None = None,
        email: str | None = None,
        success_url: str | None = None,
        fail_url: str | None = None,
        callback_url: str | None = None,
        config: AnoreConfig | None = None,
    ) -> dict[str, Any]:
        config = config or self.snapshot()
        if type(amount_kopeks) is not int or amount_kopeks <= 0:
            raise ValueError('Invalid local Anore amount')
        if methods is not None and (not methods or any(m not in ANORE_METHODS for m in methods)):
            raise ValueError('Invalid Anore payment method')
        if config.api_key.startswith('an_') and (not config.shop_id or config.shop_id <= 0):
            raise ValueError('Anore account key requires shopId')
        payload: dict[str, Any] = {
            'amount': amount_kopeks / 100,
            'currency': 'RUB',
            'description': description,
            'orderId': order_id,
        }
        if config.shop_id is not None:
            payload['shopId'] = config.shop_id
        if methods:
            payload['methods'] = methods
        if email and email.strip():
            payload['email'] = email.strip()
        for name, value in [('successurl', success_url), ('failurl', fail_url), ('callbackUrl', callback_url)]:
            if value:
                payload[name] = value
        return await self._request('POST', '/payments', payload=payload, config=config)

    async def get_payment(self, payment_id: str, *, config: AnoreConfig | None = None) -> dict[str, Any]:
        if not isinstance(payment_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', payment_id):
            raise ValueError('Invalid Anore payment ID')
        return await self._request('GET', f'/payments/{payment_id}', config=config or self.snapshot())


anore_service = AnoreService()
