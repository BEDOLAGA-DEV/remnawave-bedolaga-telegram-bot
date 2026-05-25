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
from app.services.etoplatezhi_service import etoplatezhi_service
from app.services.payment.recurring.base import ChargeResult, RecurringProvider


logger = structlog.get_logger(__name__)


GATE_BASE_URL = 'https://api.etoplatezhi.ru'
# Default method code when a saved card has no method_code recorded (old rows
# created before backfill, or unexpected providers): card-partner is the
# historical default since it was the only recurring channel originally enabled.
DEFAULT_METHOD_CODE = 'card-partner'
# Explicit mapping method_code → endpoint URL. Per EtoPlatezhi support docs
# (2026-05-25), the paths are NOT uniform — yoomoney uses /wallet/yoomoney/
# rather than the method-code segment that card-partner/sberpay follow.
_METHOD_ENDPOINTS: dict[str, str] = {
    'card-partner': '/v2/payment/card-partner/recurring',
    'sberpay': '/v2/payment/sberpay/recurring',
    'yoomoney-wallet': '/v2/payment/wallet/yoomoney/recurring',
}


def _build_recurring_endpoint(method_code: str | None) -> str:
    code = (method_code or DEFAULT_METHOD_CODE).strip()
    return _METHOD_ENDPOINTS.get(code, _METHOD_ENDPOINTS[DEFAULT_METHOD_CODE])


# Statuses returned by EtoPlatezhi for a successful charge initiation.
# (The actual settlement happens asynchronously via webhook.)
_SUCCESS_STATUSES = {'success', 'awaiting clarification', 'in process', 'in progress'}


class EtoPlatezhiRecurringProvider(RecurringProvider):
    name = 'etoplatezhi'

    def __init__(self, *, http_timeout: float = 15.0) -> None:
        self._http_timeout = http_timeout
        self._client: httpx.AsyncClient | None = None

    def _http_client(self) -> httpx.AsyncClient:
        # Reuse a single AsyncClient so the TLS connection pool to
        # api.etoplatezhi.ru survives between charges (~200ms saved per call).
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._http_timeout)
        return self._client

    def _resolve_webhook_url(self) -> str | None:
        base = getattr(settings, 'WEBHOOK_URL', None)
        if not base:
            return None
        path = getattr(settings, 'ETOPLATEZHI_WEBHOOK_PATH', '/etoplatezhi-webhook')
        return f'{base.rstrip("/")}{path}'

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

        customer_id = str(metadata.get('user_telegram_id') or user_id or '0')

        # Use the supplied idempotency key as the EtoPlatezhi `payment_id` so
        # retries collapse on the gateway side. EtoPlatezhi requires the field
        # to be unique per project — if a caller did not pass one, generate a
        # fresh UUID4.
        payment_id = idempotency_key or uuid.uuid4().hex

        # EtoPlatezhi silently discards recurring requests when
        # merchant_callback_url or customer.ip_address are missing — the ack
        # returns status:success but no transaction is ever created.
        webhook_url = self._resolve_webhook_url()

        customer_block: dict[str, Any] = {'id': customer_id}
        customer_block['ip_address'] = str(
            metadata.get('customer_ip_address')
            or metadata.get('ip_address')
            or getattr(settings, 'ETOPLATEZHI_FALLBACK_CUSTOMER_IP', '127.0.0.1')
        )

        general_block: dict[str, Any] = {
            'project_id': etoplatezhi_service.project_id,
            'payment_id': payment_id,
        }
        if webhook_url:
            general_block['merchant_callback_url'] = webhook_url

        payload: dict[str, Any] = {
            'general': general_block,
            'customer': customer_block,
            'payment': {
                'amount': amount_kopeks,
                'currency': getattr(settings, 'ETOPLATEZHI_CURRENCY', 'RUB'),
                'description': description,
            },
            'recurring': {'id': recurring_id_int},
        }
        payload['general']['signature'] = etoplatezhi_service._sign(payload)

        url = f'{GATE_BASE_URL}{_build_recurring_endpoint(metadata.get("method_code"))}'
        try:
            response = await self._http_client().post(
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
