"""Сервис для работы с API Antilopay (lk.antilopay.com/api/v1)."""

import base64
import json
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import structlog
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15

from app.config import settings


logger = structlog.get_logger(__name__)

API_BASE_URL = 'https://lk.antilopay.com/api/v1'


def validate_amount(value: Any) -> int:
    """Convert a finite, positive RUB amount into exact kopeks; never round."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError('Invalid Antilopay amount')
    try:
        amount = Decimal(str(value))
        kopeks = amount * 100
        if not amount.is_finite() or amount <= 0 or kopeks != kopeks.to_integral_value():
            raise ValueError('Invalid Antilopay amount')
        return int(kopeks)
    except InvalidOperation as error:
        raise ValueError('Invalid Antilopay amount') from error


class AntilopayAPIError(Exception):
    """Ошибка API Antilopay."""

    def __init__(self, status_code: int, message: str, code: int | None = None):
        self.status_code = status_code
        self.message = message
        self.api_code = code
        super().__init__(f'Antilopay API error ({status_code}): {message}')


class AntilopayService:
    """Сервис для работы с API Antilopay."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    @property
    def secret_id(self) -> str:
        return settings.ANTILOPAY_SECRET_ID or ''

    @property
    def private_key(self) -> str:
        return settings.ANTILOPAY_PRIVATE_KEY or ''

    @property
    def public_key(self) -> str:
        return settings.ANTILOPAY_PUBLIC_KEY or ''

    @property
    def project_id(self) -> str:
        return settings.ANTILOPAY_PROJECT_ID or ''

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

    def _sign_request(self, json_body: str) -> str:
        """SHA256WithRSA подпись JSON body приватным ключом.

        Результат — base64-encoded строка.
        """
        rsa_key = RSA.import_key(base64.b64decode(self.private_key))
        h = SHA256.new(json_body.encode('UTF-8'))
        signature = pkcs1_15.new(rsa_key).sign(h)
        return base64.b64encode(signature).decode('UTF-8')

    def _build_headers(self, json_body: str) -> dict[str, str]:
        """Строит заголовки запроса с подписью."""
        return {
            'Content-Type': 'application/json',
            'X-Apay-Secret-Id': self.secret_id,
            'X-Apay-Sign': self._sign_request(json_body),
            'X-Apay-Sign-Version': '1',
        }

    async def create_payment(
        self,
        *,
        amount_rubles: float,
        order_id: str,
        product_name: str,
        product_type: str = 'services',
        description: str = '',
        customer_email: str | None = None,
        customer_phone: str | None = None,
        prefer_methods: list[str] | None = None,
        success_url: str | None = None,
        fail_url: str | None = None,
        merchant_extra: str | None = None,
    ) -> dict[str, Any]:
        """
        Создает платеж через API Antilopay.
        POST /payment/create
        """
        validate_amount(amount_rubles)
        if settings.ANTILOPAY_CURRENCY.upper() != 'RUB':
            raise ValueError('Antilopay supports RUB only')
        payload: dict[str, Any] = {
            'project_identificator': self.project_id,
            'amount': amount_rubles,
            'order_id': order_id,
            'currency': settings.ANTILOPAY_CURRENCY.upper(),
            'product_name': product_name,
            'product_type': product_type,
            'description': description,
        }

        # customer — обязательное поле, нужен email или phone
        customer: dict[str, str] = {}
        if customer_email:
            customer['email'] = customer_email
        if customer_phone:
            customer['phone'] = customer_phone
        if not customer:
            raise ValueError('Antilopay requires customer email or phone')
        payload['customer'] = customer

        if prefer_methods:
            payload['prefer_methods'] = prefer_methods
        if success_url:
            payload['success_url'] = success_url
        if fail_url:
            payload['fail_url'] = fail_url
        if merchant_extra:
            payload['merchant_extra'] = merchant_extra[:255]

        json_body = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)

        logger.info(
            'Antilopay API create_payment',
            order_id=order_id,
            amount_rubles=amount_rubles,
            prefer_methods=prefer_methods,
        )

        try:
            session = await self._get_session()
            async with session.post(
                f'{API_BASE_URL}/payment/create',
                data=json_body,
                headers=self._build_headers(json_body),
                allow_redirects=False,
            ) as response:
                data = await response.json(content_type=None)

                if not isinstance(data, dict):
                    raise AntilopayAPIError(response.status, 'Invalid response format')
                api_code = data.get('code')
                if response.status == 200 and type(api_code) is int and api_code == 0:
                    payment_url = data.get('payment_url')
                    if not isinstance(data.get('payment_id'), str) or not data['payment_id']:
                        raise AntilopayAPIError(response.status, 'Missing payment id')
                    if (
                        not isinstance(payment_url, str)
                        or urlsplit(payment_url).scheme != 'https'
                        or not urlsplit(payment_url).hostname
                    ):
                        raise AntilopayAPIError(response.status, 'Invalid payment URL')
                    logger.info(
                        'Antilopay API payment created',
                        order_id=order_id,
                        payment_id=data.get('payment_id'),
                    )
                    return data

                error_msg = data.get('message') or data.get('error') or 'Unspecified API error'
                logger.error(
                    'Antilopay create_payment error',
                    status_code=response.status,
                    api_code=api_code,
                    error_msg=error_msg,
                )
                raise AntilopayAPIError(response.status, error_msg, api_code)

        except aiohttp.ClientError as e:
            logger.exception('Antilopay API connection error', error=e)
            raise

    async def check_payment(
        self,
        *,
        order_id: str,
    ) -> dict[str, Any]:
        """
        Проверяет статус платежа.
        POST /payment/check
        """
        payload: dict[str, Any] = {
            'project_identificator': self.project_id,
            'order_id': order_id,
        }

        json_body = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)

        logger.info('Antilopay check_payment', order_id=order_id)

        try:
            session = await self._get_session()
            async with session.post(
                f'{API_BASE_URL}/payment/check',
                data=json_body,
                headers=self._build_headers(json_body),
                allow_redirects=False,
            ) as response:
                data = await response.json(content_type=None)

                if not isinstance(data, dict):
                    raise AntilopayAPIError(response.status, 'Invalid response format')
                if response.status == 200 and data.get('code') == 0:
                    return data

                error_msg = data.get('message') or data.get('error') or 'Unspecified API error'
                logger.error(
                    'Antilopay check_payment error',
                    status_code=response.status,
                    error_msg=error_msg,
                )
                raise AntilopayAPIError(response.status, error_msg)

        except aiohttp.ClientError as e:
            logger.exception('Antilopay API connection error', error=e)
            raise

    def verify_callback_signature(self, raw_body: bytes, received_signature: str) -> bool:
        """Верификация подписи callback Antilopay через SHA256WithRSA.

        Подпись приходит в заголовке X-Apay-Callback.
        Проверяется ПУБЛИЧНЫМ ключом.
        """
        try:
            if not received_signature:
                logger.warning('Antilopay callback: отсутствует X-Apay-Callback')
                return False

            rsa_key = RSA.import_key(base64.b64decode(self.public_key))
            h = SHA256.new(raw_body)
            signature_bytes = base64.b64decode(received_signature)

            pkcs1_15.new(rsa_key).verify(h, signature_bytes)
            return True

        except (ValueError, TypeError) as e:
            logger.warning('Antilopay callback: invalid signature', error=str(e))
            return False
        except Exception as e:
            logger.error('Antilopay callback verify error', error=e)
            return False


# Singleton instance
antilopay_service = AntilopayService()
