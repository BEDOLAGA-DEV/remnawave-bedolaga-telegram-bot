"""Сервис для работы с API UnitPay (unitpay.ru)."""

import hashlib
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

API_BASE_URL = 'https://unitpay.ru/api'


class UnitPayService:
    """Сервис для работы с API UnitPay."""

    def __init__(self):
        self._project_id: int | None = None
        self._secret_key: str | None = None

    @property
    def project_id(self) -> int:
        if self._project_id is None:
            self._project_id = settings.UNITPAY_PROJECT_ID
        return self._project_id or 0

    @property
    def secret_key(self) -> str:
        if self._secret_key is None:
            self._secret_key = settings.UNITPAY_SECRET_KEY
        return self._secret_key or ''

    def _generate_signature(self, method: str, params: dict[str, Any]) -> str:
        """
        Генерирует подпись для webhook.
        Формат: sha256(method{up}sorted_params_values{up}secretKey)
        """
        sorted_keys = sorted(params.keys())
        parts = [method] + [str(params[k]) for k in sorted_keys] + [self.secret_key]
        sign_str = '{up}'.join(parts)
        return hashlib.sha256(sign_str.encode('utf-8')).hexdigest()

    def verify_webhook_signature(self, method: str, params: dict[str, Any], signature: str) -> bool:
        """
        Проверяет подпись webhook уведомления от UnitPay.
        """
        try:
            # Убираем signature из params для вычисления
            clean_params = {k: v for k, v in params.items() if k != 'signature'}
            expected = self._generate_signature(method, clean_params)
            return expected.lower() == signature.lower()
        except Exception as e:
            logger.error('UnitPay webhook verify error', error=e)
            return False

    async def create_payment(
        self,
        *,
        account: str,
        amount: float,
        description: str,
        payment_type: str = 'card',
        currency: str = 'RUB',
        result_url: str | None = None,
    ) -> dict[str, Any]:
        """
        Создает платеж через API UnitPay.
        GET /api?method=initPayment

        payment_type: card, sbp и др.
        """
        params = {
            'method': 'initPayment',
            'params[paymentType]': payment_type,
            'params[projectId]': self.project_id,
            'params[secretKey]': self.secret_key,
            'params[sum]': amount,
            'params[account]': account,
            'params[desc]': description,
            'params[currency]': currency,
        }

        if result_url:
            params['params[resultUrl]'] = result_url

        logger.info(
            'UnitPay API initPayment',
            project_id=self.project_id,
            account=account,
            amount=amount,
            payment_type=payment_type,
        )

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    API_BASE_URL,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response,
            ):
                data = await response.json()
                logger.info('UnitPay API response', data=data)

                if 'error' in data:
                    error_msg = data['error'].get('message', 'Unknown error')
                    logger.error('UnitPay initPayment error', error_msg=error_msg)
                    raise Exception(f'UnitPay API error: {error_msg}')

                result = data.get('result', {})
                return {
                    'paymentId': result.get('paymentId'),
                    'redirectUrl': result.get('redirectUrl'),
                    'receiptUrl': result.get('receiptUrl'),
                    'type': result.get('type'),
                    'message': result.get('message'),
                }

        except aiohttp.ClientError as e:
            logger.exception('UnitPay API connection error', error=e)
            raise

    async def get_payment_info(self, payment_id: int) -> dict[str, Any]:
        """
        Получает информацию о платеже.
        GET /api?method=getPayment
        """
        params = {
            'method': 'getPayment',
            'params[paymentId]': payment_id,
            'params[secretKey]': self.secret_key,
        }

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    API_BASE_URL,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response,
            ):
                data = await response.json()
                logger.info('UnitPay getPayment response', data=data)

                if 'error' in data:
                    error_msg = data['error'].get('message', 'Unknown error')
                    raise Exception(f'UnitPay API error: {error_msg}')

                return data.get('result', {})

        except aiohttp.ClientError as e:
            logger.exception('UnitPay API connection error', error=e)
            raise


# Singleton instance
unitpay_service = UnitPayService()
