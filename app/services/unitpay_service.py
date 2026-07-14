"""UnitPay API client."""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

API_URL = 'https://unitpay.ru/api'
# IPs from which UnitPay sends webhook notifications
UNITPAY_WEBHOOK_IPS: frozenset[str] = frozenset({'31.186.100.49', '51.250.20.9'})


class UnitPayService:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    @staticmethod
    def _compute_signature(method: str, params: dict[str, Any], secret_key: str) -> str:
        """sha256(method + "{up}" + sorted_param_values + "{up}" + secretKey)."""
        keys = sorted(params.keys())
        values = [str(params[k]) for k in keys]
        data = method + '{up}' + '{up}'.join(values) + '{up}' + secret_key
        return hashlib.sha256(data.encode()).hexdigest()

    def verify_webhook_signature(self, method: str, params: dict[str, Any], received_sign: str) -> bool:
        """Verify incoming webhook signature; excludes 'signature' key from params."""
        from app.config import settings

        clean = {k: v for k, v in params.items() if k != 'signature'}
        expected = self._compute_signature(method, clean, settings.UNITPAY_SECRET_KEY or '')
        return expected == received_sign

    async def init_payment(
        self,
        *,
        order_id: str,
        amount_rubles: float,
        desc: str,
        account: str,
        payment_type: str | None = None,
        currency: str = 'RUB',
        result_url: str | None = None,
        back_url: str | None = None,
        hide_other_methods: bool = True,
        subscription: bool = False,
        subscription_id: str | None = None,
        customer_email: str | None = None,
        customer_phone: str | None = None,
    ) -> dict[str, Any]:
        """
        Initiate a payment via the UnitPay API.
        When subscription_id is provided it charges the saved card directly.
        Otherwise creates a new redirect payment.
        """
        from app.config import settings

        params: dict[str, Any] = {
            'account': account,
            'currency': currency,
            'desc': desc,
            'projectId': settings.UNITPAY_PROJECT_ID,
            'sum': f'{amount_rubles:.2f}',
        }
        if subscription_id:
            params['subscriptionId'] = subscription_id
        elif payment_type:
            params['paymentType'] = payment_type

        if result_url:
            params['resultUrl'] = result_url
        if back_url:
            params['backUrl'] = back_url
        if hide_other_methods:
            params['hideOtherMethods'] = 1
        if subscription and not subscription_id:
            params['subscription'] = 'true'
        if customer_email:
            params['customerEmail'] = customer_email
        if customer_phone:
            params['customerPhone'] = customer_phone

        params['signature'] = self._compute_signature('initPayment', params, settings.UNITPAY_SECRET_KEY or '')

        flat: dict[str, Any] = {'method': 'initPayment'}
        for k, v in params.items():
            flat[f'params[{k}]'] = v

        response = await self._http().get(API_URL, params=flat)
        response.raise_for_status()
        return response.json()

    async def get_payment(self, unitpay_id: str) -> dict[str, Any]:
        """Retrieve payment info by UnitPay payment ID."""
        from app.config import settings

        flat = {
            'method': 'getPayment',
            'params[paymentId]': unitpay_id,
            'params[secretKey]': settings.UNITPAY_SECRET_KEY or '',
        }
        response = await self._http().get(API_URL, params=flat)
        response.raise_for_status()
        return response.json()


unitpay_service = UnitPayService()
