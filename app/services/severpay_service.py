"""Сервис для работы с API SeverPay (severpay.io)."""

import hashlib
import hmac
import json
import uuid
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

API_BASE_URL = 'https://severpay.io/api/merchant'


class SeverPayAPIError(Exception):
    """Ошибка API SeverPay."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f'SeverPay API error ({status_code}): {message}')


class SeverPayService:
    """Сервис для работы с API SeverPay."""

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    @property
    def mid(self) -> int | None:
        return settings.SEVERPAY_MID

    @property
    def token(self) -> str:
        return settings.SEVERPAY_TOKEN or ''

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

    def _sign_request(self, body: dict[str, Any]) -> dict[str, Any]:
        """Генерирует salt, сортирует, подписывает и возвращает body с sign."""
        body['mid'] = self.mid
        body['salt'] = uuid.uuid4().hex

        # Убираем sign если он есть (для пересчёта)
        body.pop('sign', None)

        sorted_body = dict(sorted(body.items()))
        sign = hmac.new(
            self.token.encode('utf-8'),
            json.dumps(sorted_body, ensure_ascii=False, separators=(',', ':')).encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()
        body['sign'] = sign
        return body

    async def create_payment(
        self,
        *,
        order_id: str,
        amount: float,
        currency: str = 'RUB',
        client_email: str = '',
        client_id: str = '',
        url_return: str | None = None,
        lifetime: int | None = None,
    ) -> dict[str, Any]:
        """
        Создает платеж через API SeverPay.
        POST /payin/create
        """
        payload: dict[str, Any] = {
            'order_id': order_id,
            'amount': amount,
            'currency': currency,
            'client_email': client_email,
            'client_id': client_id,
        }

        if url_return:
            payload['url_return'] = url_return
        if lifetime is not None:
            payload['lifetime'] = lifetime

        payload = self._sign_request(payload)

        logger.info(
            'SeverPay API create_payment',
            order_id=order_id,
            amount=amount,
            currency=currency,
        )

        try:
            session = await self._get_session()
            async with session.post(
                f'{API_BASE_URL}/payin/create',
                json=payload,
                headers={'Content-Type': 'application/json'},
            ) as response:
                data = await response.json(content_type=None)

                if response.status == 200 and data.get('status') is True:
                    logger.info(
                        'SeverPay API payment created',
                        order_id=order_id,
                        severpay_id=data.get('data', {}).get('id'),
                    )
                    return data.get('data', {})

                error_msg = data.get('msg') or str(data)
                logger.error(
                    'SeverPay create_payment error',
                    status_code=response.status,
                    error_msg=error_msg,
                )
                raise SeverPayAPIError(response.status, error_msg)

        except aiohttp.ClientError as e:
            logger.exception('SeverPay API connection error', error=e)
            raise

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        """
        Получает информацию о платеже.
        POST /payin/get
        """
        payload: dict[str, Any] = {
            'id': payment_id,
        }
        payload = self._sign_request(payload)

        logger.info('SeverPay get_payment', payment_id=payment_id)

        try:
            session = await self._get_session()
            async with session.post(
                f'{API_BASE_URL}/payin/get',
                json=payload,
                headers={'Content-Type': 'application/json'},
            ) as response:
                data = await response.json(content_type=None)

                if response.status == 200 and data.get('status') is True:
                    return data.get('data', {})

                error_msg = data.get('msg') or str(data)
                logger.error(
                    'SeverPay get_payment error',
                    status_code=response.status,
                    error_msg=error_msg,
                )
                raise SeverPayAPIError(response.status, error_msg)

        except aiohttp.ClientError as e:
            logger.exception('SeverPay API connection error', error=e)
            raise

    def verify_webhook_signature(self, raw_body: bytes) -> bool:
        """Верификация подписи webhook SeverPay.

        Из body убираем sign, сортируем оставшиеся ключи, считаем HMAC-SHA256.
        """
        try:
            payload = json.loads(raw_body)
            received_sign = payload.get('sign')
            if not received_sign:
                logger.warning('SeverPay webhook: отсутствует sign в теле')
                return False

            sorted_body = dict(sorted((k, v) for k, v in payload.items() if k != 'sign'))
            expected = hmac.new(
                self.token.encode('utf-8'),
                json.dumps(sorted_body, ensure_ascii=False, separators=(',', ':')).encode('utf-8'),
                hashlib.sha256,
            ).hexdigest()

            return hmac.compare_digest(expected, received_sign)
        except Exception as e:
            logger.error('SeverPay webhook verify error', error=e)
            return False


# Singleton instance
severpay_service = SeverPayService()
