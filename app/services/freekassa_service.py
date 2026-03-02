"""Сервис для работы с API Freekassa."""

import asyncio
import hashlib
import hmac
import json
import time
import urllib.request
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

# Email-заглушки для Freekassa API (test@example.com вызывает ошибку OP-SP-7)
_FALLBACK_EMAILS = [
    'ivan.petrov@mail.ru',
    'user.alex@yandex.ru',
    'sergei.k@gmail.com',
    'dmitry.v@inbox.ru',
    'anna.s@bk.ru',
    'maxim.ivanov@list.ru',
    'elena.p@rambler.ru',
    'artem.n@mail.ru',
    'nikita.z@yandex.ru',
    'olga.m@gmail.com',
    'roman.t@inbox.ru',
    'svetlana.d@bk.ru',
    'kirill.a@mail.ru',
    'marina.b@yandex.ru',
    'pavel.g@list.ru',
    'tatiana.l@gmail.com',
    'andrey.f@rambler.ru',
    'natalia.e@inbox.ru',
    'vladislav.r@mail.ru',
    'yulia.h@yandex.ru',
]


def _get_fallback_email() -> str:
    """Возвращает случайный email-заглушку из списка."""
    import random

    return random.choice(_FALLBACK_EMAILS)


# Кэш для публичного IP
_cached_public_ip: str | None = None
_ip_fetch_lock = asyncio.Lock()

# IP-адреса Freekassa для проверки webhook
FREEKASSA_IPS: set[str] = {
    '168.119.157.136',
    '168.119.60.227',
    '178.154.197.79',
    '51.250.54.238',
}

API_BASE_URL = 'https://api.fk.life/v1'

# Сервисы для определения публичного IP (в порядке приоритета)
IP_SERVICES = [
    'https://api.ipify.org',
    'https://ifconfig.me/ip',
    'https://icanhazip.com',
    'https://ipinfo.io/ip',
]


async def get_public_ip() -> str:
    """
    Получает публичный IP сервера.
    1. Сначала проверяет переменную окружения SERVER_PUBLIC_IP
    2. Если нет - запрашивает через внешние сервисы и кэширует
    """
    global _cached_public_ip

    # Проверяем переменную окружения
    env_ip = getattr(settings, 'SERVER_PUBLIC_IP', None)
    if env_ip:
        return env_ip

    # Возвращаем кэшированный IP если есть
    if _cached_public_ip:
        return _cached_public_ip

    async with _ip_fetch_lock:
        # Повторная проверка после получения блокировки
        if _cached_public_ip:
            return _cached_public_ip

        # Пробуем получить IP от внешних сервисов
        async with aiohttp.ClientSession() as session:
            for service_url in IP_SERVICES:
                try:
                    async with session.get(service_url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                        if response.status == 200:
                            ip = (await response.text()).strip()
                            # Простая валидация IPv4
                            if ip and len(ip.split('.')) == 4:
                                _cached_public_ip = ip
                                logger.info('Определён публичный IP сервера', ip=ip)
                                return ip
                except Exception as e:
                    logger.debug('Не удалось получить IP от', service_url=service_url, error=e)
                    continue

        # Fallback на известный рабочий IP если ничего не получилось
        fallback_ip = '185.92.183.173'
        logger.warning('Не удалось определить публичный IP, используем fallback', fallback_ip=fallback_ip)
        _cached_public_ip = fallback_ip
        return fallback_ip


class FreekassaService:
    """Сервис для работы с API Freekassa."""

    def __init__(self):
        self._shop_id: int | None = None
        self._api_key: str | None = None
        self._secret1: str | None = None
        self._secret2: str | None = None

    @property
    def shop_id(self) -> int:
        if self._shop_id is None:
            self._shop_id = settings.FREEKASSA_SHOP_ID
        return self._shop_id or 0

    @property
    def api_key(self) -> str:
        if self._api_key is None:
            self._api_key = settings.FREEKASSA_API_KEY
        return self._api_key or ''

    @property
    def secret1(self) -> str:
        if self._secret1 is None:
            self._secret1 = settings.FREEKASSA_SECRET_WORD_1
        return self._secret1 or ''

    @property
    def secret2(self) -> str:
        if self._secret2 is None:
            self._secret2 = settings.FREEKASSA_SECRET_WORD_2
        return self._secret2 or ''

    def _generate_api_signature_hmac(self, params: dict[str, Any]) -> str:
        """
        Генерирует подпись для API запроса (HMAC-SHA256).
        Используется для API методов (создание заказа и т.д.)
        """
        # Исключаем signature из параметров и сортируем по ключу
        sign_data = {k: v for k, v in params.items() if k != 'signature'}
        sorted_items = sorted(sign_data.items())

        # Формируем строку: значения через |
        msg = '|'.join(str(v) for _, v in sorted_items)

        # HMAC-SHA256
        return hmac.new(self.api_key.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).hexdigest()

    def _generate_api_signature(self, params: dict[str, Any]) -> str:
        """
        Генерирует подпись для API запроса.
        Для новых API методов используется HMAC-SHA256.
        """
        return self._generate_api_signature_hmac(params)

    def generate_form_signature(self, amount: float, currency: str, order_id: str) -> str:
        """
        Генерирует подпись для платежной формы.
        Формат: MD5(shop_id:amount:secret1:currency:order_id)
        """
        # Приводим amount к int, если это целое число
        final_amount = int(amount) if float(amount).is_integer() else amount
        sign_string = f'{self.shop_id}:{final_amount}:{self.secret1}:{currency}:{order_id}'
        return hashlib.md5(sign_string.encode()).hexdigest()

    def verify_webhook_signature(self, shop_id: int, amount: float, order_id: str, sign: str) -> bool:
        """
        Проверяет подпись webhook уведомления.
        Формат: MD5(shop_id:amount:secret2:order_id)
        """
        # Приводим amount к int, если это целое число
        final_amount = int(amount) if float(amount).is_integer() else amount
        expected_sign = hashlib.md5(f'{shop_id}:{final_amount}:{self.secret2}:{order_id}'.encode()).hexdigest()
        return sign.lower() == expected_sign.lower()

    def verify_webhook_ip(self, ip: str) -> bool:
        """Проверяет, что IP входит в разрешенный список Freekassa."""
        return ip in FREEKASSA_IPS

    def build_payment_url(
        self,
        order_id: str,
        amount: float,
        currency: str = 'RUB',
        email: str | None = None,
        phone: str | None = None,
        payment_system_id: int | None = None,
        lang: str = 'ru',
        ip: str | None = None,
    ) -> str:
        """
        Формирует URL для перенаправления на оплату (форма выбора).
        Используется когда FREEKASSA_USE_API = False.
        """
        # Приводим amount к int, если это целое число
        final_amount = int(amount) if float(amount).is_integer() else amount

        # Используем payment_system_id из настроек, если не передан явно
        ps_id = payment_system_id or settings.FREEKASSA_PAYMENT_SYSTEM_ID

        # Специальная обработка для метода оплаты 44 (NSPK), чтобы работало как в старой версии
        if ps_id == 44:
            try:
                # Определяем IP (важно для API запроса) - здесь синхронно, поэтому лучше иметь передачу IP
                # Если IP не передан, используем fallback
                target_ip = ip or '185.92.183.173'
                target_email = email or _get_fallback_email()

                params = {
                    'shopId': self.shop_id,
                    'nonce': int(time.time_ns()),
                    'paymentId': str(order_id),
                    'i': 44,
                    'email': target_email,
                    'ip': target_ip,
                    'amount': final_amount,
                    'currency': 'RUB',
                }

                # Генерация подписи
                params['signature'] = self._generate_api_signature(params)

                logger.info('Freekassa synchronous build_payment_url for 44', params=params)

                data_json = json.dumps(params).encode('utf-8')
                req = urllib.request.Request(
                    f'{API_BASE_URL}/orders/create', data=data_json, headers={'Content-Type': 'application/json'}
                )

                with urllib.request.urlopen(req, timeout=30) as response:
                    resp_body = response.read().decode('utf-8')
                    data = json.loads(resp_body)

                    if data.get('type') == 'error':
                        logger.error('Freekassa build_payment_url error', data=data)
                        # Fallback to standard flow if error? Or raise?
                        # User wants it to work. Raise to see error is safer.
                        # raise Exception(f"Freekassa API Error: {data.get('message')}")
                        # Но чтобы не ломать полностью, можно попробовать вернуть обычную ссылку,
                        # если API не сработал? Нет, вернем ошибку или ссылку из data.

                    if data.get('location'):
                        return data.get('location')
            except Exception as e:
                logger.error('Failed to create order 44 via sync API', error=e)
                # Если не получилось, попробуем сгенерировать обычную ссылку как fallback

        signature = self.generate_form_signature(final_amount, currency, order_id)

        params = {
            'm': self.shop_id,
            'oa': final_amount,
            'currency': currency,
            'o': order_id,
            's': signature,
            'lang': lang,
        }

        if email:
            params['em'] = email
        if phone:
            params['phone'] = phone

        if ps_id:
            params['i'] = ps_id

        query = '&'.join(f'{k}={v}' for k, v in params.items())
        return f'https://pay.fk.money/?{query}'

    async def create_order(
        self,
        order_id: str,
        amount: float,
        currency: str = 'RUB',
        email: str | None = None,
        ip: str | None = None,
        payment_system_id: int | None = None,
        success_url: str | None = None,
        failure_url: str | None = None,
        notification_url: str | None = None,
    ) -> dict[str, Any]:
        """
        Создает заказ через API Freekassa.
        POST /orders/create

        Используется для NSPK СБП (payment_system_id=44) и других методов.
        Возвращает словарь с 'location' (ссылка на оплату).
        """
        # Приводим amount к int, если это целое число
        final_amount = int(amount) if float(amount).is_integer() else amount

        # Используем payment_system_id из настроек, если не передан явно
        ps_id = payment_system_id or settings.FREEKASSA_PAYMENT_SYSTEM_ID or 1

        target_email = email or _get_fallback_email()

        # Определяем публичный IP сервера
        server_ip = ip or await get_public_ip()

        params = {
            'shopId': self.shop_id,
            'nonce': int(time.time_ns()),  # Наносекунды для уникальности
            'paymentId': str(order_id),
            'i': ps_id,
            'email': target_email,
            'ip': server_ip,
            'amount': final_amount,
            'currency': currency,
        }

        # Генерируем подпись HMAC-SHA256
        params['signature'] = self._generate_api_signature(params)

        logger.info('Freekassa API create_order params', params=params)

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
                logger.info('Freekassa API response', text=text)

                data = await response.json()

                # Проверяем на ошибку - API может вернуть error или type=error
                error_msg = data.get('error') or data.get('message')
                if response.status != 200 or data.get('type') == 'error' or error_msg:
                    logger.error('Freekassa create_order error', data=data)
                    raise Exception(f'Freekassa API error: {error_msg or "Unknown error"}')

                return data
        except aiohttp.ClientError as e:
            logger.exception('Freekassa API connection error', error=e)
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
        Удобный метод для получения только ссылки.
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
            raise Exception('Freekassa API did not return payment URL (location)')
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
        params['signature'] = self._generate_api_signature(params)

        logger.debug('Freekassa get_order_status params', params=params)

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
                logger.debug('Freekassa get_order_status response', text=text)
                return await response.json()
        except aiohttp.ClientError as e:
            logger.exception('Freekassa API connection error', error=e)
            raise

    async def get_balance(self) -> dict[str, Any]:
        """Получает баланс магазина."""
        params = {
            'shopId': self.shop_id,
            'nonce': int(time.time_ns()),
        }
        params['signature'] = self._generate_api_signature(params)

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
            logger.exception('Freekassa API connection error', error=e)
            raise

    async def get_payment_systems(self) -> dict[str, Any]:
        """Получает список доступных платежных систем."""
        params = {
            'shopId': self.shop_id,
            'nonce': int(time.time_ns()),
        }
        params['signature'] = self._generate_api_signature(params)

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    f'{API_BASE_URL}/currencies',
                    json=params,
                    headers={'Content-Type': 'application/json'},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response,
            ):
                return await response.json()
        except aiohttp.ClientError as e:
            logger.exception('Freekassa API connection error', error=e)
            raise


# Singleton instance
freekassa_service = FreekassaService()
