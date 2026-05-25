"""EtoPlatezhi adapter for the recurring-provider abstraction.

Charges saved cards via the EtoPlatezhi Gate API endpoint
``POST /v2/payment/card-partner/recurring``. The card-on-file is identified
by ``recurring.id``, obtained when the initial Payment Page transaction was
registered with ``stored_card_type = 3`` (registration of automatic
charges).

Spec references:
* https://developers.etoplatezhi.ru/ru/ru_gate__cof_merchant_side.html
* https://developers.etoplatezhi.ru/ru/ru_gate_payment_recurring_registration.html
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import structlog

from app.config import settings
from app.services.payment.recurring.base import ChargeResult, RecurringProvider


logger = structlog.get_logger(__name__)


GATE_BASE_URL = 'https://api.etoplatezhi.ru'
# Default method code if a saved card has no method_code recorded (old rows
# created before backfill, or unexpected providers): card-partner is the
# historical default since it was the only recurring channel originally enabled.
DEFAULT_METHOD_CODE = 'card-partner'
# Supported method codes → URL path segment after /v2/payment/{...}/recurring.
# Method codes match EtoPlatezhi's terminal.method_code values.
_SUPPORTED_METHOD_CODES = {'card-partner', 'sberpay', 'yoomoney-wallet'}


def _build_recurring_endpoint(method_code: str | None) -> str:
    code = (method_code or DEFAULT_METHOD_CODE).strip()
    if code not in _SUPPORTED_METHOD_CODES:
        code = DEFAULT_METHOD_CODE
    return f'/v2/payment/{code}/recurring'

# Statuses returned by EtoPlatezhi for a successful charge initiation.
# (The actual settlement happens asynchronously via webhook.)
_SUCCESS_STATUSES = {'success', 'awaiting clarification', 'in process', 'in progress'}


class EtoPlatezhiRecurringProvider(RecurringProvider):
    name = 'etoplatezhi'

    def __init__(self, *, http_timeout: float = 15.0) -> None:
        self._http_timeout = http_timeout

    def is_enabled(self) -> bool:
        if not getattr(settings, 'ETOPLATEZHI_ENABLED', False):
            return False
        if not getattr(settings, 'ETOPLATEZHI_RECURRENT_ENABLED', False):
            return False
        return bool(settings.ETOPLATEZHI_PROJECT_ID and settings.ETOPLATEZHI_SECRET_KEY)

    async def charge(
        self,
        *,
        provider_token: str,
        amount_kopeks: int,
        description: str,
        metadata: dict[str, Any],
        idempotency_key: str,
        user_id: int | None = None,
    ) -> ChargeResult:
        try:
            recurring_id_int = int(provider_token)
        except (TypeError, ValueError):
            return ChargeResult(
                success=False,
                error_code='invalid_token',
                error_message=f'EtoPlatezhi recurring_id must be numeric, got: {provider_token!r}',
            )

        from app.services.etoplatezhi_service import etoplatezhi_service

        customer_id = str(metadata.get('user_telegram_id') or user_id or '0')

        # Use the supplied idempotency key as the EtoPlatezhi `payment_id` so
        # retries collapse on the gateway side. EtoPlatezhi requires the field
        # to be unique per project — if a caller did not pass one, generate a
        # fresh UUID4.
        payment_id = idempotency_key or uuid.uuid4().hex

        payload: dict[str, Any] = {
            'general': {
                'project_id': etoplatezhi_service.project_id,
                'payment_id': payment_id,
            },
            'customer': {'id': customer_id},
            'payment': {
                'amount': amount_kopeks,
                'currency': getattr(settings, 'ETOPLATEZHI_CURRENCY', 'RUB'),
                'description': description,
            },
            'recurring': {'id': recurring_id_int},
        }
        payload['general']['signature'] = etoplatezhi_service._sign(payload)

        # EtoPlatezhi exposes a distinct recurring endpoint per payment method
        # (card-partner / sberpay / yoomoney-wallet). Resolve from metadata —
        # callers should pass the saved card's method_code; missing values
        # fall back to card-partner (historical default).
        endpoint = _build_recurring_endpoint(metadata.get('method_code'))
        url = f'{GATE_BASE_URL}{endpoint}'
        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
                )
        except Exception as exc:  # pragma: no cover - network errors
            logger.error(
                'etoplatezhi_recurring_charge_exception',
                user_id=user_id,
                recurring_id=recurring_id_int,
                error=str(exc),
            )
            return ChargeResult(success=False, error_code='http_error', error_message=str(exc))

        try:
            data = response.json()
        except Exception:
            data = {'raw_text': response.text[:500]}

        if response.status_code >= 400:
            logger.warning(
                'etoplatezhi_recurring_charge_http_error',
                user_id=user_id,
                recurring_id=recurring_id_int,
                http_status=response.status_code,
                response=data,
            )
            return ChargeResult(
                success=False,
                error_code=f'http_{response.status_code}',
                error_message=str(data),
                raw=data if isinstance(data, dict) else {},
            )

        operation = (data or {}).get('operation') if isinstance(data, dict) else None
        op_status = (operation or {}).get('status') or (data or {}).get('status', '')
        op_status_normalised = str(op_status).strip().lower()

        if op_status_normalised in _SUCCESS_STATUSES:
            return ChargeResult(
                success=True,
                provider_payment_id=str((operation or {}).get('id') or payment_id),
                raw=data if isinstance(data, dict) else {},
            )

        return ChargeResult(
            success=False,
            error_code='charge_declined',
            error_message=f'status={op_status_normalised or "unknown"}',
            raw=data if isinstance(data, dict) else {},
        )
