"""Сервис для работы с API Paydex (merchant API v1, paydex.pro)."""

import hashlib
import hmac
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

# Какие способы оплаты Paydex понимает в поле `method` при создании счёта.
PAYDEX_METHODS = ('sbp', 'card', 'crypto')


class PaydexAPIError(Exception):
    """Ошибка API Paydex."""

    def __init__(self, status_code: int, message: str, code: str | None = None) -> None:
        self.status_code = status_code
        self.message = message
        self.code = code
        super().__init__(f'Paydex API error ({status_code}): {message}')


def kopeks_to_rubles(amount_kopeks: int) -> str:
    """Копейки → строка рублей с двумя знаками.

    Paydex принимает суммы строкой («100.00»), а бот считает в копейках.
    Через Decimal, чтобы 10_05 не превратились в «100.49999999999999».
    """
    return str(
        (Decimal(int(amount_kopeks)) / Decimal(100)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    )


def rubles_to_kopeks(amount: Any) -> int:
    """Строка рублей из ответа Paydex → копейки."""
    return int(
        (Decimal(str(amount or '0')) * Decimal(100)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    )


class PaydexService:
    """Клиент Paydex Merchant API v1 (paydex.pro).

    Аутентификация — Bearer-токен секретного ключа проекта (``sk_live_…`` или
    ``sk_test_…``; режим определяется самим ключом, отдельного флага нет).
    Суммы в запросах и ответах — строки рублей с двумя знаками.

    Вебхук подписывается отдельным секретом проекта (не API-ключом!):
    заголовок ``X-Paydex-Signature`` = ``sha256=<HMAC-SHA256(сырое тело)>``.
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    @property
    def base_url(self) -> str:
        return (settings.PAYDEX_BASE_URL or 'https://paydex.pro').rstrip('/')

    @property
    def api_key(self) -> str:
        return settings.PAYDEX_API_KEY or ''

    @property
    def webhook_secret(self) -> str:
        return settings.PAYDEX_WEBHOOK_SECRET or ''

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

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        }
        if idempotency_key:
            # Повтор запроса с тем же ключом вернёт тот же счёт, а не создаст второй.
            headers['Idempotency-Key'] = idempotency_key[:128]
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        url = f'{self.base_url}/api/v1/{path.lstrip("/")}'
        try:
            session = await self._get_session()
            async with session.request(
                method,
                url,
                json=json_payload,
                params=params,
                headers=self._headers(idempotency_key),
            ) as response:
                data = await response.json(content_type=None)
                if response.status >= 400:
                    # Формат ошибок v1: {"error": {"code": "...", "message": "...", "details": [...]}}
                    error = data.get('error') if isinstance(data, dict) else None
                    message = (error or {}).get('message') if isinstance(error, dict) else None
                    code = (error or {}).get('code') if isinstance(error, dict) else None
                    logger.error(
                        'Paydex API error',
                        url=url,
                        status=response.status,
                        code=code,
                        detail=message or data,
                    )
                    raise PaydexAPIError(response.status, str(message or data), code)
                return data if isinstance(data, dict) else {'_raw': data}
        except aiohttp.ClientError as error:
            logger.exception('Paydex API connection error', url=url, error=error)
            raise

    async def create_invoice(
        self,
        *,
        amount_kopeks: int,
        order_id: str,
        method: str | None = None,
        description: str | None = None,
        success_url: str | None = None,
        fail_url: str | None = None,
        customer_id: str | None = None,
        email: str | None = None,
        expire_minutes: int | None = None,
    ) -> dict[str, Any]:
        """Создаёт счёт в Paydex.

        ``POST /api/v1/invoices`` → 201 с объектом счёта: ``id`` (UUID),
        ``status`` (``created``), ``url`` — страница оплаты, ``payableAmount`` —
        сколько заплатит покупатель (с его долей комиссии, если она на нём).

        ``method`` — ``sbp`` / ``card`` / ``crypto``; если не передать, покупатель
        выберет способ сам на странице оплаты. ``order_id`` уникален в пределах
        проекта: повторный запрос с тем же ``order_id`` и тем же телом вернёт
        существующий счёт (200 вместо 201), а с другой суммой — 409.
        """
        payload: dict[str, Any] = {
            'amount': kopeks_to_rubles(amount_kopeks),
            'orderId': order_id,
        }
        if method:
            if method not in PAYDEX_METHODS:
                raise ValueError(f'Paydex: неизвестный способ оплаты {method!r}')
            payload['method'] = method
        if description:
            payload['description'] = description[:500]
        if success_url:
            payload['successUrl'] = success_url[:500]
        if fail_url:
            payload['failUrl'] = fail_url[:500]
        if expire_minutes:
            payload['expire'] = int(expire_minutes)
        customer: dict[str, str] = {}
        if customer_id:
            customer['userId'] = str(customer_id)[:190]
        if email:
            customer['email'] = email[:190]
        if customer:
            payload['customer'] = customer

        logger.info(
            'Paydex API create_invoice',
            order_id=order_id,
            amount_kopeks=amount_kopeks,
            method=method or 'any',
        )

        data = await self._request(
            'POST', '/invoices', json_payload=payload, idempotency_key=order_id
        )

        if not data.get('id'):
            logger.error('Paydex create_invoice: в ответе нет id счёта', response_data=data)
            raise PaydexAPIError(200, f'Incomplete create invoice response: {data}')
        if not data.get('url'):
            # Ссылка есть всегда, но если вдруг нет — счёт уже создан, запись сохраняем:
            # иначе пришедший позже вебхук не найдёт платёж и деньги придётся сверять руками.
            logger.warning(
                'Paydex create_invoice: ответ без ссылки оплаты',
                order_id=order_id,
                invoice_id=data.get('id'),
            )

        logger.info(
            'Paydex API invoice created',
            order_id=order_id,
            invoice_id=data.get('id'),
            status=data.get('status'),
        )
        return data

    async def check_invoice(
        self,
        *,
        invoice_id: str | None = None,
        order_id: str | None = None,
    ) -> dict[str, Any]:
        """Получает статус счёта.

        По ``invoice_id`` — ``GET /api/v1/invoices/{id}``; по ``order_id`` —
        ``GET /api/v1/invoices?orderId=…`` (список из одного элемента).
        Статусы: ``created`` / ``pending`` / ``paid`` / ``failed`` / ``expired`` /
        ``refunded`` / ``partially_refunded``.
        """
        if invoice_id:
            logger.info('Paydex check_invoice', invoice_id=invoice_id)
            return await self._request('GET', f'/invoices/{invoice_id}')
        if not order_id:
            raise ValueError('Paydex check_invoice: нужен invoice_id или order_id')

        logger.info('Paydex check_invoice', order_id=order_id)
        data = await self._request('GET', '/invoices', params={'orderId': order_id})
        items = data.get('items') if isinstance(data, dict) else None
        if isinstance(items, list) and items:
            return items[0]
        return {}

    async def get_methods(self) -> dict[str, Any]:
        """Доступные способы оплаты проекта и ставки: ``GET /api/v1/methods``."""
        return await self._request('GET', '/methods')

    async def get_balance(self) -> dict[str, Any]:
        """Баланс мерчанта: ``GET /api/v1/balance`` (в USDT)."""
        return await self._request('GET', '/balance')

    def verify_webhook_signature(self, raw_body: bytes, signature: str | None) -> bool:
        """Верификация подписи вебхука.

        ``X-Paydex-Signature`` = ``sha256=<HMAC-SHA256 hex>`` от СЫРОГО тела
        запроса; ключ — секрет вебхуков проекта (``PAYDEX_WEBHOOK_SECRET``),
        он отличается от API-ключа.
        """
        try:
            received = (signature or '').strip()
            if not received:
                logger.warning('Paydex webhook: отсутствует X-Paydex-Signature')
                return False

            if not self.webhook_secret:
                # Без секрета HMAC считался бы от b'' — подпись подделал бы кто угодно
                logger.error('Paydex webhook: не задан секрет вебхуков, проверка невозможна')
                return False

            expected = hmac.new(
                self.webhook_secret.encode('utf-8'), raw_body, hashlib.sha256
            ).hexdigest()
            # Принимаем и «sha256=<hex>», и просто «<hex>»: так подпись можно
            # проверить вручную, не разбираясь в префиксе.
            candidate = received.split('=', 1)[1] if received.lower().startswith('sha256=') else received

            if not hmac.compare_digest(candidate.lower(), expected):
                logger.warning('Paydex webhook: подпись не совпала')
                return False
            return True
        except Exception as error:  # noqa: BLE001 — подпись не должна ронять обработчик
            logger.exception('Paydex webhook: ошибка проверки подписи', error=error)
            return False


paydex_service = PaydexService()
