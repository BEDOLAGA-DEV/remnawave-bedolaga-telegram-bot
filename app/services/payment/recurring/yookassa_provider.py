"""YooKassa adapter for the recurring-provider abstraction.

Wraps the existing :class:`~app.services.yookassa_service.YooKassaService`
``create_autopayment`` call. Pure adapter — no behavioural changes vs. the
previous direct call site in ``recurrent_payment_service``.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.config import settings
from app.services.payment.recurring.base import ChargeResult, RecurringProvider


logger = structlog.get_logger(__name__)


class YooKassaRecurringProvider(RecurringProvider):
    name = 'yookassa'

    def __init__(self) -> None:
        self._service = None

    def _get_service(self):
        """Lazy-instantiate the YooKassa service so import order does not matter."""
        if self._service is not None:
            return self._service
        if not settings.is_yookassa_enabled():
            return None
        from app.services.yookassa_service import YooKassaService

        self._service = YooKassaService()
        return self._service

    def is_enabled(self) -> bool:
        if not getattr(settings, 'YOOKASSA_RECURRENT_ENABLED', False):
            return False
        service = self._get_service()
        return bool(service and getattr(service, 'configured', False))

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
        service = self._get_service()
        if not service:
            return ChargeResult(success=False, error_message='YooKassa is not configured')

        amount_rubles = round(amount_kopeks / 100, 2)
        try:
            result = await service.create_autopayment(
                amount=amount_rubles,
                currency='RUB',
                description=description,
                payment_method_id=provider_token,
                metadata=metadata,
                idempotence_key=idempotency_key,
            )
        except Exception as exc:  # pragma: no cover - network/runtime errors
            logger.error(
                'yookassa_recurring_charge_exception',
                user_id=user_id,
                provider_token_prefix=provider_token[:8] if provider_token else None,
                error=str(exc),
            )
            return ChargeResult(success=False, error_message=str(exc))

        if not result:
            return ChargeResult(success=False, error_message='create_autopayment returned None')

        return ChargeResult(
            success=True,
            provider_payment_id=str(result.get('id') or ''),
            raw=result,
        )
