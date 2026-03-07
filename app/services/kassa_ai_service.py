"""Сервис для работы с API KassaAI (api.fk.life)."""

import asyncio
import hashlib
import hmac
import time
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

# Sub-method to payment_system_id mapping
KASSA_AI_SUB_METHODS = {
    'kassa_ai_sbp': {'payment_system_id': 44},
    'kassa_ai_card': {'payment_system_id': 36},
}

# Кэш для публичного IP
_cached_public_ip: str | None = None
_ip_fetch_lock = asyncio.Lock()

API_BASE_URL = 'https://api.fk.life/v1'

# Сервисы для определения публичного IP
IP_SERVICES = [
    'https://api.ipify.org',
    'https://ifconfig.me/ip',
    'https://icanhazip.com',
    'https://ipinfo.io/ip',
]


async def get_public_ip() -> str:
    """
    Получает публичный IP сервера.
    1. Проверяет переменную окружения SERVER_PUBLIC_IP
    2. Если нет - запрашивает через внешние сервисы и кэширует
    """
    global _cached_public_ip

    env_ip = getattr(settings, 'SERVER_PUBLIC_IP', None)
    if env_ip:
        return env_ip

    if _cached_public_ip:
        return _cached_public_ip

    async with _ip_fetch_lock:
        if _cached_public_ip:
            return _cached_public_ip

        async with aiohttp.ClientSession() as session:
            for service_url in IP_SERVICES:
                try:
                    async with session.get(service_url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                        if response.status == 200:
                            ip = (await response.text()).strip()
                            if ip and len(ip.split('.')) == 4:
                                _cached_public_ip = ip
                                logger.info('KassaAI: определён публичный IP сервера', ip=ip)
                                return ip
                except Exception as e:
                    logger.debug('KassaAI: не удалось получить IP от', service_url=service_url, error=e)
                    continue

        fallback_ip = '127.0.0.1'
        logger.warning('KassaAI: не удалось определить публичный IP, используем fallback', fallback_ip=fallback_ip)
        _cached_public_ip = fallback_ip
        return fallback_ip


class KassaAiService:
    """Сервис для работы с API KassaAI."""

    def __init__(self):
        self._shop_id: int | None = None
        self._api_key: str | None = None
        self._secret2: str | None = None

    @property
    def shop_id(self) -> int:
        if self._shop_id is None:
            self._shop_id = settings.KASSA_AI_SHOP_ID
        return self._shop_id or 0

    @property
    def api_key(self) -> str:
        if self._api_key is None:
            self._api_key = settings.KASSA_AI_API_KEY
        return self._api_key or ''

    @property
    def secret2(self) -> str:
        if self._secret2 is None:
            self._secret2 = settings.KASSA_AI_SECRET_WORD_2
        return self._secret2 or ''

    def _generate_hmac_signature(self, params: dict[str, Any]) -> str:
        """
        Генерирует подпись для API запроса (HMAC-SHA256).
        Сортирует ключи, соединяет значения через |
        """
        sign_data = {k: v for k, v in params.items() if k != 'signature'}
        sorted_keys = sorted(sign_data.keys())
        msg = '|'.join(str(sign_data[k]) for k in sorted_keys)

        return hmac.new(self.api_key.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).hexdigest()

    def verify_webhook_signature(self, shop_id: int, amount: float, order_id: str, sign: str) -> bool:
        """
        Проверяет подпись webhook уведомления.
        Формат: MD5(shop_id:amount:secret2:order_id)
        """
        try:
            # Приводим amount к строке без лишних нулей
            if isinstance(amount, float) and amount.is_integer():
                amount_str = str(int(amount))
            else:
                amount_str = str(amount)

            sign_str = f'{shop_id}:{amount_str}:{self.secret2}:{order_id}'
            expected_sign = hashlib.md5(sign_str.encode('utf-8')).hexdigest()

            return expected_sign.lower() == sign.lower()
        except Exception as e:
            logger.error('KassaAI webhook verify error', error=e)
            return False

    async def create_order(
        self,
        order_id: str,
        amount: float,
        currency: str = 'RUB',
        email: str | None = None,
        ip: str | None = None,
        payment_system_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Создает заказ через API KassaAI.
        POST /orders/create

        payment_system_id:
        - 44 = СБП (QR код)
        - 36 = Банковские карты РФ
        - 43 = SberPay
        """
        # Приводим amount к int, если это целое число
        final_amount = int(amount) if float(amount).is_integer() else amount

        # Payment system из настроек или default (44 = СБП)
        ps_id = payment_system_id or settings.KASSA_AI_PAYMENT_SYSTEM_ID or 44

        # Email: используем telegram-формат если не указан
        target_email = email or f'user_{order_id}@telegram.org'

        # Определяем публичный IP сервера
        server_ip = ip or await get_public_ip()

        params = {
            'shopId': self.shop_id,
            'nonce': int(time.time_ns()),
            'paymentId': str(order_id),
            'i': ps_id,
            'email': target_email,
            'ip': server_ip,
            'amount': final_amount,
            'currency': currency,
        }

        # Генерируем подпись HMAC-SHA256
        params['signature'] = self._generate_hmac_signature(params)

        logger.info(
            'KassaAI API create_order: shop_id=, order_id=, amount=, ps_id',
            shop_id=self.shop_id,
            order_id=order_id,
            final_amount=final_amount,
            ps_id=ps_id,
        )

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    f'{API_BASE_URL}/orders/create',
                    json=params,
                    headers={'Content-Type': 'application/json'},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response,
            ):
                text = await response.text()
                logger.info('KassaAI API response', text=text)

                data = await response.json()

                # Проверяем на ошибку
                if data.get('type') == 'error':
                    error_msg = data.get('error') or data.get('message') or 'Unknown error'
                    logger.error('KassaAI create_order error', error_msg=error_msg)
                    raise Exception(f'KassaAI API error: {error_msg}')

                if data.get('type') == 'success':
                    return {
                        'location': data.get('location'),
                        'orderId': data.get('orderId'),
                        'paymentId': data.get('paymentId'),
                    }

                # Неизвестный формат ответа
                logger.error('KassaAI unexpected response', data=data)
                raise Exception('KassaAI unexpected response format')

        except aiohttp.ClientError as e:
            logger.exception('KassaAI API connection error', error=e)
            raise

    async def create_order_and_get_url(
        self,
        order_id: str,
        amount: float,
        currency: str = 'RUB',
        email: str | None = None,
        ip: str | None = None,
        payment_system_id: int | None = None,
    ) -> str:
        """
        Создает заказ через API и возвращает URL для оплаты.
        """
        result = await self.create_order(
            order_id=order_id,
            amount=amount,
            currency=currency,
            email=email,
            ip=ip,
            payment_system_id=payment_system_id,
        )
        location = result.get('location')
        if not location:
            raise Exception('KassaAI API did not return payment URL (location)')
        return location

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        """
        Получает статус заказа.
        POST /orders
        """
        params = {
            'shopId': self.shop_id,
            'nonce': int(time.time_ns()),
            'paymentId': str(order_id),
        }
        params['signature'] = self._generate_hmac_signature(params)

        logger.info('KassaAI get_order_status: order_id', order_id=order_id)

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    f'{API_BASE_URL}/orders',
                    json=params,
                    headers={'Content-Type': 'application/json'},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response,
            ):
                text = await response.text()
                logger.info('KassaAI get_order_status response', text=text)
                return await response.json()
        except aiohttp.ClientError as e:
            logger.exception('KassaAI API connection error', error=e)
            raise

    async def get_balance(self) -> dict[str, Any]:
        """Получает баланс магазина."""
        params = {
            'shopId': self.shop_id,
            'nonce': int(time.time_ns()),
        }
        params['signature'] = self._generate_hmac_signature(params)

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    f'{API_BASE_URL}/balance',
                    json=params,
                    headers={'Content-Type': 'application/json'},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response,
            ):
                return await response.json()
        except aiohttp.ClientError as e:
            logger.exception('KassaAI API connection error', error=e)
            raise


# Singleton instance
kassa_ai_service = KassaAiService()
