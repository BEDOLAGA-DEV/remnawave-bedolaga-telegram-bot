"""UnitPay adapter for the recurring-provider abstraction.

UnitPay рекуррент работает через тот же initPayment API с параметром
`subscriptionId` (ID подписки, полученный при первом платеже с subscription=true).
"""

from __future__ import annotations

from typing import Any

import structlog

from app.config import settings
from app.services.payment.recurring.base import ChargeResult, RecurringProvider
from app.services.unitpay_service import unitpay_service

logger = structlog.get_logger(__name__)


class UnitPayRecurringProvider(RecurringProvider):
    name = 'unitpay'

    def is_enabled(self) -> bool:
        return (
            getattr(settings, 'UNITPAY_ENABLED', False)
            and getattr(settings, 'UNITPAY_RECURRENT_ENABLED', False)
            and settings.is_unitpay_enabled()
        )

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
        """
        Charge via UnitPay recurring subscription.
        provider_token = subscriptionId from initial payment webhook.
        """
        subscription_id = provider_token.strip()
        if not subscription_id:
            return ChargeResult(
                success=False,
                error_code='invalid_token',
                error_message='UnitPay subscriptionId is empty',
            )

        amount_rubles = amount_kopeks / 100
        order_id = idempotency_key or f'upr_{user_id}_{amount_kopeks}'
        account = str(user_id)  # stable customer ID for UnitPay anti-fraud; webhook looked up by paymentId not account

        customer_email = metadata.get('customer_email') or None

        try:
            result = await unitpay_service.init_payment(
                order_id=order_id,
                amount_rubles=amount_rubles,
                desc=description,
                account=account,
                currency=getattr(settings, 'UNITPAY_CURRENCY', 'RUB'),
                subscription_id=subscription_id,
                result_url=settings.get_unitpay_result_url(),
                back_url=settings.get_unitpay_back_url(),
                customer_email=customer_email,
            )

            response = result.get('response') or {}
            payment_id = str(response.get('paymentId', ''))
            status = str(response.get('status', '')).lower()

            if response and payment_id and status not in ('error', 'reject', 'declined'):
                return ChargeResult(
                    success=True,
                    provider_payment_id=payment_id,
                    raw=result if isinstance(result, dict) else {},
                )

            error = result.get('error') or {}
            return ChargeResult(
                success=False,
                error_code=str(error.get('code', 'charge_declined')),
                error_message=str(error.get('message', f'status={status}')),
                raw=result if isinstance(result, dict) else {},
            )

        except Exception as exc:
            logger.error('unitpay_recurring_charge_exception', user_id=user_id, error=str(exc))
            return ChargeResult(success=False, error_code='http_error', error_message=str(exc))
