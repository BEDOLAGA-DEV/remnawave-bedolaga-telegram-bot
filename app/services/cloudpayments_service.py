"""High level service for interacting with CloudPayments API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from typing import Any
from urllib.parse import unquote_plus

import httpx

from app.config import settings


logger = logging.getLogger(__name__)


class CloudPaymentsAPIError(RuntimeError):
    """Raised when the CloudPayments API returns an error response."""

    def __init__(self, message: str, reason_code: int | None = None):
        super().__init__(message)
        self.reason_code = reason_code


class CloudPaymentsService:
    """Wrapper around the CloudPayments REST API for balance top-ups."""

    def __init__(
        self,
        *,
        public_id: str | None = None,
        api_secret: str | None = None,
        api_url: str | None = None,
    ) -> None:
        self.public_id = public_id or settings.CLOUDPAYMENTS_PUBLIC_ID
        self.api_secret = api_secret or settings.CLOUDPAYMENTS_API_SECRET
        self.api_url = (api_url or settings.CLOUDPAYMENTS_API_URL).rstrip('/')

    @property
    def is_configured(self) -> bool:
        return bool(settings.is_cloudpayments_enabled() and self.public_id and self.api_secret)

    def _get_auth_header(self) -> str:
        """Generate Basic Auth header for CloudPayments API."""
        if not self.public_id or not self.api_secret:
            raise CloudPaymentsAPIError('CloudPayments credentials not configured')
        credentials = f'{self.public_id}:{self.api_secret}'
        encoded = base64.b64encode(credentials.encode()).decode()
        return f'Basic {encoded}'

    def _build_headers(self) -> dict[str, str]:
        return {
            'Authorization': self._get_auth_header(),
            'Content-Type': 'application/json',
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make a request to CloudPayments API."""
        if not self.is_configured:
            raise CloudPaymentsAPIError('CloudPayments service is not configured')

        url = f'{self.api_url}/{path.lstrip("/")}'

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.request(
                    method,
                    url,
                    json=json,
                    headers=self._build_headers(),
                )

                data = response.json()

                if response.status_code >= 400:
                    logger.error('CloudPayments API error %s: %s', response.status_code, data)
                    raise CloudPaymentsAPIError(f'CloudPayments API returned status {response.status_code}')

                return data

        except httpx.RequestError as error:
            logger.error('Error communicating with CloudPayments API: %s', error)
            raise CloudPaymentsAPIError('Failed to communicate with CloudPayments API') from error

    @staticmethod
    def _amount_from_kopeks(amount_kopeks: int) -> float:
        """Convert kopeks to rubles."""
        return amount_kopeks / 100

    @staticmethod
    def _amount_to_kopeks(amount: float) -> int:
        """Convert rubles to kopeks."""
        return int(amount * 100)

    async def generate_payment_link(
        self,
        telegram_id: int | None,
        user_id: int,
        amount_kopeks: int,
        invoice_id: str,
        description: str | None = None,
        email: str | None = None,
        success_redirect_url: str | None = None,
        fail_redirect_url: str | None = None,
    ) -> str:
        """
        Create a payment order via CloudPayments API and return payment URL.

        Args:
            telegram_id: User's Telegram ID (optional, used in JsonData)
            user_id: Internal user ID (used as AccountId for tracking)
            amount_kopeks: Amount in kopeks
            invoice_id: Unique invoice ID for this payment
            description: Payment description
            email: User's email (optional)
            success_redirect_url: Redirect URL after successful payment
            fail_redirect_url: Redirect URL after failed payment

        Returns:
            URL to CloudPayments payment page
        """
        if not self.is_configured:
            raise CloudPaymentsAPIError('CloudPayments is not configured')

        amount = self._amount_from_kopeks(amount_kopeks)

        # Формируем данные для создания заказа через API /orders/create
        # AccountId uses user_id for consistency (works for both Telegram and email users)
        payload: dict[str, Any] = {
            'Amount': amount,
            'Currency': settings.CLOUDPAYMENTS_CURRENCY,
            'Description': description or settings.CLOUDPAYMENTS_DESCRIPTION,
            'AccountId': str(user_id),
            'InvoiceId': invoice_id,
            'JsonData': {
                'user_id': user_id,
                'telegram_id': telegram_id,
                'invoice_id': invoice_id,
            },
        }

        if email:
            payload['Email'] = email

        if settings.CLOUDPAYMENTS_REQUIRE_EMAIL:
            payload['RequireConfirmation'] = False

        # URL для редиректа после оплаты
        if success_redirect_url or settings.CLOUDPAYMENTS_RETURN_URL:
            payload['SuccessRedirectUrl'] = success_redirect_url or settings.CLOUDPAYMENTS_RETURN_URL

        if fail_redirect_url:
            payload['FailRedirectUrl'] = fail_redirect_url

        # Создаём заказ через API
        response = await self._request('POST', '/orders/create', json=payload)

        if not response.get('Success'):
            error_message = response.get('Message', 'Unknown error')
            logger.error('CloudPayments orders/create failed: %s', error_message)
            raise CloudPaymentsAPIError(f'Failed to create order: {error_message}')

        model = response.get('Model', {})
        payment_url = model.get('Url')

        if not payment_url:
            logger.error('CloudPayments orders/create returned no URL: %s', response)
            raise CloudPaymentsAPIError('CloudPayments API returned no payment URL')

        logger.info(
            'CloudPayments order created: id=%s, url=%s',
            model.get('Id'),
            payment_url,
        )

        return payment_url

    def generate_invoice_id(self, user_id: int) -> str:
        """Generate unique invoice ID for a payment using internal user ID."""
        return f'cp_{user_id}_{int(time.time())}'

    async def charge_by_token(
        self,
        token: str,
        amount_kopeks: int,
        account_id: str,
        invoice_id: str,
        description: str | None = None,
    ) -> dict[str, Any]:
        """
        Charge a payment using saved card token (recurrent payment).

        Args:
            token: Card token from previous payment
            amount_kopeks: Amount in kopeks
            account_id: User's account ID (telegram_id)
            invoice_id: Unique invoice ID
            description: Payment description

        Returns:
            CloudPayments API response
        """
        amount = self._amount_from_kopeks(amount_kopeks)

        payload = {
            'Amount': amount,
            'Currency': settings.CLOUDPAYMENTS_CURRENCY,
            'AccountId': account_id,
            'Token': token,
            'InvoiceId': invoice_id,
            'Description': description or settings.CLOUDPAYMENTS_DESCRIPTION,
        }

        return await self._request('POST', '/payments/tokens/charge', json=payload)

    async def get_payment(self, transaction_id: int) -> dict[str, Any]:
        """Get payment details by CloudPayments transaction ID."""
        return await self._request(
            'POST',
            '/payments/get',
            json={'TransactionId': transaction_id},
        )

    async def find_payment(self, invoice_id: str) -> dict[str, Any]:
        """Find payment by invoice ID."""
        return await self._request(
            'POST',
            '/payments/find',
            json={'InvoiceId': invoice_id},
        )

    async def refund_payment(
        self,
        transaction_id: int,
        amount_kopeks: int | None = None,
    ) -> dict[str, Any]:
        """
        Refund a payment (full or partial).

        Args:
            transaction_id: CloudPayments transaction ID
            amount_kopeks: Amount to refund in kopeks (None for full refund)

        Returns:
            CloudPayments API response
        """
        payload: dict[str, Any] = {'TransactionId': transaction_id}

        if amount_kopeks is not None:
            payload['Amount'] = self._amount_from_kopeks(amount_kopeks)

        return await self._request('POST', '/payments/refund', json=payload)

    async def void_payment(self, transaction_id: int) -> dict[str, Any]:
        """Cancel an authorized but not captured payment."""
        return await self._request(
            'POST',
            '/payments/void',
            json={'TransactionId': transaction_id},
        )

    @staticmethod
    def verify_webhook_signature(body: bytes, signature: str, api_secret: str) -> bool:
        """
        Verify CloudPayments webhook signature.

        CloudPayments uses two different HMAC headers:
        - Content-HMAC: calculated from URL-encoded body (raw)
        - X-Content-HMAC: calculated from URL-decoded body

        This method tries both variants to ensure compatibility.

        Args:
            body: Raw request body bytes
            signature: Signature from X-Content-HMAC or Content-HMAC header
            api_secret: CloudPayments API secret

        Returns:
            True if signature is valid
        """
        if not signature or not api_secret:
            return False

        def calc_hmac(data: bytes) -> str:
            return base64.b64encode(
                hmac.new(
                    api_secret.encode(),
                    data,
                    hashlib.sha256,
                ).digest()
            ).decode()

        # Try with raw (URL-encoded) body first (for Content-HMAC)
        calculated_raw = calc_hmac(body)
        if hmac.compare_digest(calculated_raw, signature):
            return True

        # Try with URL-decoded body (for X-Content-HMAC)
        calculated_decoded = None
        try:
            decoded_body = unquote_plus(body.decode('utf-8')).encode('utf-8')
            calculated_decoded = calc_hmac(decoded_body)
            if hmac.compare_digest(calculated_decoded, signature):
                return True
        except Exception:
            pass

        logger.warning(
            'CloudPayments signature mismatch: expected_raw=%s..., expected_decoded=%s..., got=%s...',
            calculated_raw[:20],
            calculated_decoded[:20] if calculated_decoded else 'N/A',
            signature[:20],
        )
        return False

    @staticmethod
    def parse_webhook_data(form_data: dict[str, Any]) -> dict[str, Any]:
        """
        Parse webhook form data into structured format.

        Args:
            form_data: Form data from webhook request

        Returns:
            Parsed payment data
        """
        return {
            'transaction_id': int(form_data.get('TransactionId', 0)),
            'amount': float(form_data.get('Amount', 0)),
            'currency': form_data.get('Currency', 'RUB'),
            'invoice_id': form_data.get('InvoiceId', ''),
            'account_id': form_data.get('AccountId', ''),
            'token': form_data.get('Token'),
            'card_first_six': form_data.get('CardFirstSix'),
            'card_last_four': form_data.get('CardLastFour'),
            'card_type': form_data.get('CardType'),
            'card_exp_date': form_data.get('CardExpDate'),
            'email': form_data.get('Email'),
            'status': form_data.get('Status', ''),
            'test_mode': form_data.get('TestMode') == '1' or form_data.get('TestMode') == 'True',
            'reason': form_data.get('Reason'),
            'reason_code': int(form_data.get('ReasonCode', 0)) if form_data.get('ReasonCode') else None,
            'card_holder_message': form_data.get('CardHolderMessage'),
            'auth_code': form_data.get('AuthCode'),  # Auth code present only in Pay notifications
            'data': form_data.get('Data'),  # JSON string with custom data
        }
