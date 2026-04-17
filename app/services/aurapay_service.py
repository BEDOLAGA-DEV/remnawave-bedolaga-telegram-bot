"""Сервис для работы с API AuraPay (aurapay.tech)."""

import hashlib
import hmac
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

API_BASE_URL = 'https://app.aurapay.tech'


class AuraPayAPIError(Exception):
    """Ошибка API AuraPay."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f'AuraPay API error ({status_code}): {message}')


class AuraPayService:
    """Сервис для работы с API AuraPay."""

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    @property
    def api_key(self) -> str:
        return settings.AURAPAY_API_KEY or ''

    @property
    def shop_id(self) -> str:
        return settings.AURAPAY_SHOP_ID or ''

    @property
    def secret_key(self) -> str:
        return settings.AURAPAY_SECRET_KEY or ''

    async def _get_session(self) -> aiohttp.ClientSession:
        """Возвращает переиспользуемую HTTP-сессию."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self) -> None:
        """Закрывает HTTP-сессию."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _build_headers(self) -> dict[str, str]:
        """Строит заголовки запроса с X-ApiKey и X-ShopId."""
        return {
            'Content-Type': 'application/json',
            'X-ApiKey': self.api_key,
            'X-ShopId': self.shop_id,
        }

    async def create_invoice(
        self,
        *,
        amount: float,
        order_id: str,
        comment: str = '',
        service: str | None = None,
        success_url: str | None = None,
        fail_url: str | None = None,
        callback_url: str | None = None,
        custom_fields: str | None = None,
        lifetime: int | None = None,
    ) -> dict[str, Any]:
        """
        Создает инвойс через API AuraPay.
        POST /invoice/create
        """
        payload: dict[str, Any] = {
            'amount': amount,
            'order_id': order_id,
        }

        if comment:
            payload['comment'] = comment
        if service:
            payload['service'] = service
        if success_url:
            payload['success_url'] = success_url
        if fail_url:
            payload['fail_url'] = fail_url
        if callback_url:
            payload['callback_url'] = callback_url
        if custom_fields:
            payload['custom_fields'] = custom_fields
        if lifetime is not None:
            payload['lifetime'] = lifetime

        logger.info(
            'AuraPay API create_invoice',
            order_id=order_id,
            amount=amount,
            service=service,
        )

        try:
            session = await self._get_session()
            async with session.post(
                f'{API_BASE_URL}/invoice/create',
                json=payload,
                headers=self._build_headers(),
            ) as response:
                data = await response.json(content_type=None)

                if response.status == 200:
                    logger.info(
                        'AuraPay API invoice created',
                        order_id=order_id,
                        invoice_id=data.get('id'),
                        status=data.get('status'),
                    )
                    return data

                error_msg = data.get('message') or data.get('error') or str(data)
                logger.error(
                    'AuraPay create_invoice error',
                    status_code=response.status,
                    error_msg=error_msg,
                    response_data=data,
                )
                raise AuraPayAPIError(response.status, error_msg)

        except aiohttp.ClientError as e:
            logger.exception('AuraPay API connection error', error=e)
            raise

    async def get_invoice_status(
        self,
        *,
        order_id: str | None = None,
        invoice_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Получает статус инвойса.
        POST /invoice/status
        """
        if not order_id and not invoice_id:
            raise ValueError('Either order_id or invoice_id must be provided')

        payload: dict[str, str] = {}
        if order_id:
            payload['order_id'] = order_id
        if invoice_id:
            payload['id'] = invoice_id

        logger.info('AuraPay get_invoice_status', order_id=order_id, invoice_id=invoice_id)

        try:
            session = await self._get_session()
            async with session.post(
                f'{API_BASE_URL}/invoice/status',
                json=payload,
                headers=self._build_headers(),
            ) as response:
                data = await response.json(content_type=None)

                if response.status == 200:
                    return data

                error_msg = data.get('message') or data.get('error') or str(data)
                logger.error(
                    'AuraPay get_invoice_status error',
                    status_code=response.status,
                    error_msg=error_msg,
                )
                raise AuraPayAPIError(response.status, error_msg)

        except aiohttp.ClientError as e:
            logger.exception('AuraPay API connection error', error=e)
            raise

    def verify_webhook_signature(self, payload: dict[str, Any], received_signature: str) -> bool:
        """Верификация подписи webhook AuraPay через HMAC-SHA256.

        Algorithm: sort JSON keys alphabetically, concatenate all VALUES
        (converted to str) into one string, HMAC-SHA256 with secret key #2.
        Header: X-SIGNATURE
        """
        try:
            if not received_signature:
                logger.warning('AuraPay webhook: отсутствует X-SIGNATURE')
                return False

            # Сортируем ключи по алфавиту и конкатенируем значения
            sorted_keys = sorted(payload.keys())
            concatenated_values = ''.join(str(payload[key]) for key in sorted_keys)

            expected = hmac.new(
                self.secret_key.encode('utf-8'),
                concatenated_values.encode('utf-8'),
                hashlib.sha256,
            ).hexdigest()

            return hmac.compare_digest(expected, received_signature)
        except Exception as e:
            logger.error('AuraPay webhook verify error', error=e)
            return False


# Singleton instance
aurapay_service = AuraPayService()
