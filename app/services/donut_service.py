"""Сервис для работы с API Donut (Donut P2P, gw.donut.business)."""

import hashlib
import hmac
import json
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class DonutAPIError(Exception):
    """Ошибка API Donut."""

    def __init__(self, status_code: int, message: str, code: str | None = None) -> None:
        self.status_code = status_code
        self.message = message
        self.api_code = code
        super().__init__(f'Donut API error ({status_code}): {message}')


class DonutService:
    """Клиент для Donut P2P (gw.donut.business)."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    @property
    def base_url(self) -> str:
        return (settings.DONUT_BASE_URL or 'https://gw.donut.business').rstrip('/')

    @property
    def token(self) -> str:
        return settings.DONUT_TOKEN or ''

    @property
    def secret(self) -> str:
        return settings.DONUT_SECRET or ''

    @property
    def method_id(self) -> str | None:
        value = (settings.DONUT_METHOD_ID or '').strip()
        return value or None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    @staticmethod
    def _build_signature_string(parts: list[tuple[str, Any]]) -> str:
        """Собирает каноническую строку для подписи: имя=значение (без разделителя)."""
        chunks: list[str] = []
        for key, value in parts:
            if value is None:
                continue
            if isinstance(value, bool):
                chunks.append(f'{key}={"true" if value else "false"}')
            else:
                value_str = str(value)
                if value_str == '':
                    continue
                chunks.append(f'{key}={value_str}')
        return ''.join(chunks)

    def _hmac_hex(self, message: str) -> str:
        """HMAC-SHA256 в hex."""
        return hmac.new(
            self.secret.encode('utf-8'),
            msg=message.encode('utf-8'),
            digestmod=hashlib.sha256,
        ).hexdigest()

    def _sign_payin(self, payload: dict[str, Any]) -> str:
        amount = payload['amount']
        customer = payload['customer']
        parts: list[tuple[str, Any]] = [
            ('token', payload['token']),
            ('order_id', payload['order_id']),
            ('amount.value', amount['value']),
            ('amount.currency', amount['currency']),
            ('customer.id', customer['id']),
            ('redirect', payload['redirect']),
        ]
        return self._hmac_hex(self._build_signature_string(parts))

    def _sign_status(self, payload: dict[str, Any]) -> str:
        parts: list[tuple[str, Any]] = [
            ('token', payload['token']),
            ('transaction_id', payload['transaction_id']),
        ]
        return self._hmac_hex(self._build_signature_string(parts))

    def _sign_balance(self, payload: dict[str, Any]) -> str:
        parts: list[tuple[str, Any]] = [
            ('token', payload['token']),
        ]
        return self._hmac_hex(self._build_signature_string(parts))

    @staticmethod
    def _format_amount(amount_rubles: float) -> str:
        """Сумма строго '0.00' с точкой (требование Donut P2P)."""
        return f'{float(amount_rubles):.2f}'

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f'{self.base_url}/{path.lstrip("/")}'
        body = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
        try:
            session = await self._get_session()
            async with session.post(
                url,
                data=body,
                headers={'Content-Type': 'application/json'},
            ) as response:
                data = await response.json(content_type=None)
                return data if isinstance(data, dict) else {'_raw': data}
        except aiohttp.ClientError as error:
            logger.exception('Donut API connection error', url=url, error=error)
            raise

    async def create_payment(
        self,
        *,
        amount_rubles: float,
        order_id: str,
        customer_id: str,
        method_description: str,
        customer_email: str | None = None,
        customer_phone: str | None = None,
        callback_url: str | None = None,
        return_url: str | None = None,
        receipt: str | None = None,
        redirect: bool = True,
    ) -> dict[str, Any]:
        """Создаёт платёж (PayIn) через Donut P2P.

        POST /p2p_payin
        """
        payload: dict[str, Any] = {
            'token': self.token,
            'order_id': order_id,
            'amount': {
                'value': self._format_amount(amount_rubles),
                'currency': (settings.DONUT_CURRENCY or 'RUB').upper(),
            },
            'customer': {'id': str(customer_id)},
            'redirect': 'true' if redirect else 'false',
            'description': method_description,
        }

        if customer_email:
            payload['customer']['email'] = customer_email
        if customer_phone:
            payload['customer']['phone'] = customer_phone

        if self.method_id:
            payload['method_id'] = self.method_id
        if callback_url:
            payload['callback_url'] = callback_url
        if return_url:
            payload['return_url'] = return_url
        if receipt:
            payload['receipt'] = receipt[:255]

        payload['signature'] = self._sign_payin(payload)

        logger.info(
            'Donut API create_payment',
            order_id=order_id,
            amount_rubles=amount_rubles,
            description=method_description,
        )

        data = await self._post('/p2p_payin', payload)
        status_obj = (data.get('status') or {}) if isinstance(data, dict) else {}
        status_type = status_obj.get('type')

        if status_type in ('processing', 'success', 'created'):
            logger.info(
                'Donut API payment created',
                order_id=order_id,
                transaction_id=data.get('transaction_id'),
                status_type=status_type,
            )
            return data

        error_code = status_obj.get('error_code') or '0'
        error_msg = status_obj.get('error_description') or status_obj.get('message') or 'Unknown error'
        logger.error(
            'Donut create_payment error',
            error_code=error_code,
            error_msg=error_msg,
            response_data=data,
        )
        raise DonutAPIError(200, error_msg, error_code)

    async def check_payment(self, *, transaction_id: str) -> dict[str, Any]:
        """Получает статус платежа.

        POST /p2p_status
        """
        payload: dict[str, Any] = {
            'token': self.token,
            'transaction_id': str(transaction_id),
        }
        payload['signature'] = self._sign_status(payload)

        logger.info('Donut check_payment', transaction_id=transaction_id)
        return await self._post('/p2p_status', payload)

    async def get_balance(self) -> dict[str, Any]:
        """Получает баланс продавца.

        POST /p2p_balance
        """
        payload: dict[str, Any] = {'token': self.token}
        payload['signature'] = self._sign_balance(payload)
        return await self._post('/p2p_balance', payload)

    def verify_callback_signature(self, payload: dict[str, Any]) -> bool:
        """Верификация подписи callback (HMAC-SHA256, hex)."""
        try:
            received = (payload.get('signature') or '').strip()
            if not received:
                logger.warning('Donut callback: отсутствует signature')
                return False

            amount = payload.get('amount') or {}
            status_obj = payload.get('status') or {}
            parts: list[tuple[str, Any]] = [
                ('token', payload.get('token')),
                ('transaction_id', payload.get('transaction_id')),
                ('order_id', payload.get('order_id')),
                ('amount.value', amount.get('value')),
                ('amount.currency', amount.get('currency')),
                ('recalculated', payload.get('recalculated')),
                ('status.type', status_obj.get('type')),
            ]
            expected = self._hmac_hex(self._build_signature_string(parts))
            if not hmac.compare_digest(expected.lower(), received.lower()):
                logger.warning(
                    'Donut callback: invalid signature',
                    expected_prefix=expected[:8],
                    received_prefix=received[:8],
                )
                return False
            return True
        except Exception as error:
            logger.error('Donut callback verify error', error=error)
            return False


# Singleton instance
donut_service = DonutService()
