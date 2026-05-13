"""Сервис для работы с API Jupiter (FPGate P2P v2.1, app.juppiter.tech)."""

import hashlib
import hmac
import json
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class JupiterAPIError(Exception):
    """Ошибка API Jupiter."""

    def __init__(self, status_code: int, message: str, code: str | None = None) -> None:
        self.status_code = status_code
        self.message = message
        self.api_code = code
        super().__init__(f'Jupiter API error ({status_code}): {message}')


class JupiterService:
    """Клиент для FPGate P2P v2.1 (Jupiter / app.juppiter.tech)."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    @property
    def base_url(self) -> str:
        return (settings.JUPITER_BASE_URL or 'https://app.juppiter.tech').rstrip('/')

    @property
    def token(self) -> str:
        return settings.JUPITER_TOKEN or ''

    @property
    def secret(self) -> str:
        return settings.JUPITER_SECRET or ''

    @property
    def method_id(self) -> str | None:
        value = (settings.JUPITER_METHOD_ID or '').strip()
        return value or None

    @property
    def method_description(self) -> str:
        return (settings.JUPITER_METHOD_DESCRIPTION or 'SBP').strip() or 'SBP'

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
        """Собирает каноническую строку для подписи: имя=значение... в порядке полей.

        По спецификации FPGate P2P v2.1: «Если поле подписываемое, но не обязательное,
        то оно входит в подпись, если оно присутствует в запросе и имеет непустое значение».
        """
        chunks: list[str] = []
        for key, value in parts:
            if value is None:
                continue
            if isinstance(value, bool):
                chunks.append(f'{key}={"true" if value else "false"}')
                continue
            value_str = str(value)
            if value_str == '':
                continue
            chunks.append(f'{key}={value_str}')
        return ''.join(chunks)

    def _hmac_hex(self, message: str) -> str:
        """HMAC-SHA256 в hex (регистр не важен по спецификации)."""
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
        """Сумма строго '0.00' с точкой-разделителем (требование P2P v2.1)."""
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
            logger.exception('Jupiter API connection error', url=url, error=error)
            raise

    async def create_payment(
        self,
        *,
        amount_rubles: float,
        order_id: str,
        customer_id: str,
        customer_email: str | None = None,
        customer_phone: str | None = None,
        customer_name: str | None = None,
        callback_url: str | None = None,
        receipt: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Создаёт платёж (PayIn) согласно FPGate P2P v2.1.

        POST /p2p_payin_v2.1
        """
        payload: dict[str, Any] = {
            'token': self.token,
            'order_id': order_id,
            'amount': {
                'value': self._format_amount(amount_rubles),
                'currency': (settings.JUPITER_CURRENCY or 'RUB').upper(),
            },
            'customer': {
                'id': str(customer_id),
                'email': customer_email or settings.JUPITER_FALLBACK_EMAIL or 'user@vpn.bot',
                'phone': customer_phone or settings.JUPITER_FALLBACK_PHONE or '0000000000',
                'name': customer_name or settings.JUPITER_FALLBACK_NAME or 'User',
            },
            'redirect': 'false',
            'description': (description or self.method_description)[:255],
        }

        if self.method_id:
            payload['method_id'] = self.method_id
        if callback_url:
            payload['callback_url'] = callback_url
        if receipt:
            payload['receipt'] = receipt[:255]

        payload['signature'] = self._sign_payin(payload)

        logger.info('Jupiter API create_payment', order_id=order_id, amount_rubles=amount_rubles)

        data = await self._post('/p2p_payin_v2.1', payload)
        status = (data.get('status') or {}) if isinstance(data, dict) else {}
        status_type = status.get('type')

        if status_type in ('processing', 'success'):
            logger.info(
                'Jupiter API payment created',
                order_id=order_id,
                transaction_id=data.get('transaction_id'),
                status_type=status_type,
            )
            return data

        error_code = status.get('error_code') or '0'
        error_msg = status.get('error_description') or 'Unknown error'
        logger.error(
            'Jupiter create_payment error',
            error_code=error_code,
            error_msg=error_msg,
            response_data=data,
        )
        raise JupiterAPIError(200, error_msg, error_code)

    async def check_payment(self, *, transaction_id: str) -> dict[str, Any]:
        """Получает статус платежа.

        POST /p2p_status_v2.1
        """
        payload: dict[str, Any] = {
            'token': self.token,
            'transaction_id': str(transaction_id),
        }
        payload['signature'] = self._sign_status(payload)

        logger.info('Jupiter check_payment', transaction_id=transaction_id)
        data = await self._post('/p2p_status_v2.1', payload)
        return data

    async def get_balance(self) -> dict[str, Any]:
        """Получает баланс продавца.

        POST /p2p_balance_v2.1
        """
        payload: dict[str, Any] = {'token': self.token}
        payload['signature'] = self._sign_balance(payload)
        data = await self._post('/p2p_balance_v2.1', payload)
        return data

    def verify_callback_signature(self, payload: dict[str, Any]) -> bool:
        """Верификация подписи callback (HMAC-SHA256, hex)."""
        try:
            received = (payload.get('signature') or '').strip()
            if not received:
                logger.warning('Jupiter callback: отсутствует signature')
                return False

            amount = payload.get('amount') or {}
            status = payload.get('status') or {}
            parts: list[tuple[str, Any]] = [
                ('token', payload.get('token')),
                ('transaction_id', payload.get('transaction_id')),
                ('order_id', payload.get('order_id')),
                ('amount.value', amount.get('value')),
                ('amount.currency', amount.get('currency')),
                ('recalculated', payload.get('recalculated')),
                ('status.type', status.get('type')),
            ]
            expected = self._hmac_hex(self._build_signature_string(parts))
            if not hmac.compare_digest(expected.lower(), received.lower()):
                logger.warning(
                    'Jupiter callback: invalid signature',
                    expected_prefix=expected[:8],
                    received_prefix=received[:8],
                )
                return False
            return True
        except Exception as error:
            logger.error('Jupiter callback verify error', error=error)
            return False


# Singleton instance
jupiter_service = JupiterService()
