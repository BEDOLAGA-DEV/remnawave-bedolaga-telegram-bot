"""Сервис для работы с API LOLZ (lzt-market.com / prod-api.lzt.market)."""

import hmac
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

API_BASE_URL = 'https://prod-api.lzt.market'


class LolzAPIError(Exception):
    """Ошибка API LOLZ."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f'LOLZ API error ({status_code}): {message}')


class LolzService:
    """Сервис для работы с API LOLZ."""

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    @property
    def api_token(self) -> str:
        return settings.LOLZ_API_TOKEN or ''

    @property
    def merchant_id(self) -> int | None:
        return settings.LOLZ_MERCHANT_ID

    @property
    def webhook_secret(self) -> str:
        return settings.LOLZ_WEBHOOK_SECRET or ''

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
        """Строит заголовки запроса с Bearer-токеном."""
        return {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_token}',
        }

    async def create_invoice(
        self,
        *,
        amount: float,
        payment_id: str,
        comment: str,
        url_success: str,
        url_callback: str | None = None,
        currency: str = 'rub',
        lifetime: int | None = None,
        additional_data: str | None = None,
        required_telegram_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Создает инвойс через API LOLZ.
        POST /invoice
        """
        if self.merchant_id is None:
            raise LolzAPIError(0, 'LOLZ_MERCHANT_ID не задан')

        payload: dict[str, Any] = {
            'currency': currency,
            'amount': amount,
            'payment_id': payment_id,
            'comment': comment,
            'url_success': url_success,
            'merchant_id': self.merchant_id,
        }

        if url_callback:
            payload['url_callback'] = url_callback
        if lifetime is not None:
            payload['lifetime'] = lifetime
        if additional_data is not None:
            payload['additional_data'] = additional_data
        if required_telegram_id is not None:
            payload['required_telegram_id'] = required_telegram_id

        logger.info(
            'LOLZ API create_invoice',
            payment_id=payment_id,
            amount=amount,
            currency=currency,
        )

        try:
            session = await self._get_session()
            async with session.post(
                f'{API_BASE_URL}/invoice',
                json=payload,
                headers=self._build_headers(),
            ) as response:
                data = await response.json(content_type=None)

                if response.status == 200:
                    invoice = data.get('invoice') if isinstance(data, dict) else None
                    if not isinstance(invoice, dict):
                        logger.error(
                            'LOLZ create_invoice: missing invoice block in response',
                            response_data=data,
                        )
                        raise LolzAPIError(response.status, 'Missing invoice block in response')

                    logger.info(
                        'LOLZ API invoice created',
                        payment_id=payment_id,
                        invoice_id=invoice.get('invoice_id'),
                        status=invoice.get('status'),
                    )
                    return invoice

                error_msg = (
                    (data.get('message') if isinstance(data, dict) else None)
                    or (data.get('error') if isinstance(data, dict) else None)
                    or str(data)
                )
                logger.error(
                    'LOLZ create_invoice error',
                    status_code=response.status,
                    error_msg=error_msg,
                    response_data=data,
                )
                raise LolzAPIError(response.status, error_msg)

        except aiohttp.ClientError as e:
            logger.exception('LOLZ API connection error', error=e)
            raise

    async def get_invoice_status(self, invoice_id: int) -> dict[str, Any]:
        """
        Получает статус инвойса.
        GET /invoice/{invoice_id}
        """
        if invoice_id is None:
            raise ValueError('invoice_id must be provided')

        logger.info('LOLZ get_invoice_status', invoice_id=invoice_id)

        try:
            session = await self._get_session()
            async with session.get(
                f'{API_BASE_URL}/invoice/{invoice_id}',
                headers=self._build_headers(),
            ) as response:
                data = await response.json(content_type=None)

                if response.status == 200:
                    invoice = data.get('invoice') if isinstance(data, dict) else None
                    if isinstance(invoice, dict):
                        return invoice
                    return data if isinstance(data, dict) else {}

                error_msg = (
                    (data.get('message') if isinstance(data, dict) else None)
                    or (data.get('error') if isinstance(data, dict) else None)
                    or str(data)
                )
                logger.error(
                    'LOLZ get_invoice_status error',
                    status_code=response.status,
                    error_msg=error_msg,
                )
                raise LolzAPIError(response.status, error_msg)

        except aiohttp.ClientError as e:
            logger.exception('LOLZ API connection error', error=e)
            raise

    def verify_webhook_signature(self, received_secret: str) -> bool:
        """Верификация подписи webhook LOLZ.

        Сравнение через `hmac.compare_digest` (constant-time):
        заголовок `x-secret-key` должен совпадать с webhook secret мерчанта,
        который задаётся в merchant panel LZT отдельно от API-токена.
        """
        try:
            expected = self.webhook_secret
            if not received_secret or not expected:
                logger.warning('LOLZ webhook: отсутствует x-secret-key или LOLZ_WEBHOOK_SECRET не задан')
                return False
            return hmac.compare_digest(received_secret, expected)
        except Exception as e:
            logger.error('LOLZ webhook verify error', error=e)
            return False


# Singleton instance
lolz_service = LolzService()
