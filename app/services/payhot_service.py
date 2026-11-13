"""Bounded PayHot transport; durable invoice/retry policy belongs to PaymentService."""

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import UUID

import aiohttp

from app.config import settings


# Failed/cancelled payments can succeed later; partial captures are not full payment.
CHECKABLE_STATUSES = frozenset(
    (
        'creating',
        'create_unknown',
        'requires_payment_method',
        'requires_action',
        'processing',
        'authorized',
        'partially_captured',
        'manual_reconciliation',
        'failed',
        'cancelled',
    )
)
MAX_BODY_BYTES = 65536
METHODS = frozenset(('card', 'sbp', 'sberpay', 'crypto', 'applepay', 'googlepay'))


@dataclass(frozen=True)
class PayHotConfig:
    api_key: str = field(repr=False)
    base_url: str = 'https://app.pay.hot/api/v2'
    request_timeout: float = 20


class PayHotAPIError(Exception):
    """No raw response, credential, checkout URL or upstream exception is exposed."""

    def __init__(self, status_code: int, retry_after: int | None = None):
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(f'PayHot request failed ({status_code})')


def verify_signature(raw_body: bytes, signature: str, secrets: list[str], *, now: float | None = None) -> bool:
    match = re.fullmatch(r't=([0-9]{1,12}),v2=([a-f0-9]{64})', signature)
    if not match:
        return False
    timestamp, supplied = match.groups()
    if abs((time.time() if now is None else now) - int(timestamp)) > 300:
        return False
    signed = timestamp.encode('ascii') + b'.' + raw_body
    valid = False
    for secret in secrets:
        if secret:
            expected = hmac.new(secret.encode('utf-8'), signed, hashlib.sha256).hexdigest()
            valid |= hmac.compare_digest(expected, supplied)
    return valid


def _reject_constant(_value):
    raise ValueError('Invalid JSON number')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field')
        result[key] = value
    return result


def _identifier(value):
    if not isinstance(value, str):
        raise ValueError('Invalid PayHot payment identifier')
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise ValueError('Invalid PayHot payment identifier') from None
    return value


def _minor(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[1-9][0-9]{0,18}', value)) and int(value) <= 2**63 - 1


def _idempotency(value):
    if not isinstance(value, str) or not re.fullmatch(r'[!-~]{16,128}', value):
        raise ValueError('Invalid PayHot idempotency key')
    return value


def _https_url(value):
    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise ValueError('Invalid PayHot HTTPS URL')
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError('Invalid PayHot HTTPS URL')
    return value


class PayHotService:
    @staticmethod
    def snapshot() -> PayHotConfig:
        return PayHotConfig(
            api_key=getattr(settings, 'PAYHOT_API_KEY', '') or '',
            base_url=getattr(settings, 'PAYHOT_API_URL', 'https://app.pay.hot/api/v2'),
            request_timeout=getattr(settings, 'PAYHOT_REQUEST_TIMEOUT', 20),
        )

    @staticmethod
    def _base_url(config):
        value = config.base_url.rstrip('/')
        if value not in ('https://app.pay.hot/api/v2', 'https://gapi.pay.hot/api/v2'):
            raise ValueError('Invalid PayHot API origin')
        return value

    @staticmethod
    def _prefix(config):
        prefix = 'phk_v2_'
        if (
            not isinstance(config.api_key, str)
            or not config.api_key.startswith(prefix)
            or not re.fullmatch(r'[!-~]+', config.api_key)
        ):
            raise ValueError('Invalid PayHot environment credentials')
        return '/payments'

    @staticmethod
    def _validate_payload(payload):
        if not isinstance(payload, dict) or payload.get('currency') != 'RUB' or not _minor(payload.get('amount_minor')):
            raise ValueError('PayHot requires a positive RUB minor-unit amount')
        method = payload.get('payment_method')
        if not isinstance(method, str) or method not in METHODS:
            raise ValueError('Invalid PayHot payment method')
        reference = payload.get('merchant_order_reference')
        if not isinstance(reference, str) or not 1 <= len(reference) <= 128 or reference.strip() != reference:
            raise ValueError('Invalid PayHot order reference')
        if 'recurring_enrollment' in payload:
            raise ValueError('Recurring consent is not supported by ordinary PayHot checkout')
        for key in ('success_return_url', 'failure_return_url'):
            if key in payload:
                _https_url(payload[key])
        customer = payload.get('customer')
        if 'customer' in payload:
            if (
                not isinstance(customer, dict)
                or not isinstance(customer.get('id'), str)
                or not re.fullmatch(r'[!-~]{1,128}', customer['id'])
            ):
                raise ValueError('PayHot customer requires a stable identifier')
        if method == 'sberpay':
            if (
                not isinstance(customer, dict)
                or not isinstance(customer.get('phone'), str)
                or not re.fullmatch(r'\+7[0-9]{10}', customer['phone'])
            ):
                raise ValueError('SberPay requires the payer phone')
            if customer.get('sberpay') != {'front_type': 'back2app'}:
                raise ValueError('SberPay requires back2app checkout')

    async def _request(
        self,
        method,
        path,
        *,
        config,
        payload=None,
        idempotency_key=None,
        response_validator=None,
        success_statuses=(200, 201),
    ):
        url = self._base_url(config) + path
        if (
            isinstance(config.request_timeout, bool)
            or not isinstance(config.request_timeout, (int, float))
            or not 0 < config.request_timeout <= 60
        ):
            raise ValueError('Invalid PayHot request timeout')
        headers = {'Authorization': f'Bearer {config.api_key}', 'Accept': 'application/json'}
        if idempotency_key is not None:
            headers['Idempotency-Key'] = _idempotency(idempotency_key)
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
            if len(body) > MAX_BODY_BYTES:
                raise ValueError('PayHot request is too large')
            headers['Content-Type'] = 'application/json'
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=config.request_timeout)) as session:
                async with session.request(method, url, data=body, headers=headers, allow_redirects=False) as response:
                    retry = response.headers.get('Retry-After', '')
                    retry_after = min(int(retry), 86400) if re.fullmatch(r'[0-9]{1,8}', retry) else None
                    if response.status not in success_statuses:
                        raise PayHotAPIError(response.status, retry_after)
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        raw.extend(chunk)
                        if len(raw) > MAX_BODY_BYTES:
                            raise PayHotAPIError(response.status)
                    try:
                        result = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
                        if response_validator is not None:
                            response_validator(result)
                        else:
                            payment = result['payment']
                            if not isinstance(payment, dict):
                                raise ValueError
                            _identifier(payment['id'])
                            if payment.get('currency') != 'RUB' or not _minor(payment.get('amount_minor')):
                                raise ValueError
                            if not isinstance(payment.get('merchant_order_reference'), str) or not isinstance(
                                payment.get('status'), str
                            ):
                                raise ValueError
                            checkout = result.get('checkout')
                            if checkout is not None:
                                if not isinstance(checkout, dict):
                                    raise ValueError
                                if 'url' in checkout:
                                    _https_url(checkout['url'])
                                elif (
                                    checkout.get('access_token_available') is not False
                                    or checkout.get('reissue_required') is not True
                                ):
                                    raise ValueError
                    except (ValueError, TypeError, KeyError, UnicodeDecodeError, RecursionError):
                        raise PayHotAPIError(response.status) from None
                    return result
        except (aiohttp.ClientError, TimeoutError):
            raise PayHotAPIError(0) from None

    async def create_payment(self, payload: dict, idempotency_key: str, *, config: PayHotConfig):
        self._validate_payload(payload)
        return await self._request(
            'POST', self._prefix(config), config=config, payload=payload, idempotency_key=idempotency_key
        )

    async def get_payment(self, payment_id: str, *, config: PayHotConfig):
        return await self._request('GET', f'{self._prefix(config)}/{_identifier(payment_id)}', config=config)

    async def reissue_checkout(self, payment_id: str, *, idempotency_key: str, config: PayHotConfig):
        return await self._request(
            'POST',
            f'{self._prefix(config)}/{_identifier(payment_id)}/checkout-sessions',
            config=config,
            idempotency_key=None,
        )


payhot_service = PayHotService()
