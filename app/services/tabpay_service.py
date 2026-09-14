"""Клиент TabPay (tabpay.org): создание платежей и проверка подписи вебхуков.

TabPay принимает оплату по СБП и картам (3-D Secure). Мерчант создаёт платёж,
уводит покупателя на payUrl, а результат получает подписанным вебхуком.
Все суммы — целые копейки, песочница отдельным магазином.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class TabPayAPIError(Exception):
    """API TabPay ответил ошибкой (4xx/5xx)."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f'TabPay API error ({status_code}): {message}')


class TabPayNetworkError(Exception):
    """Ответ не получен: обрыв соединения или таймаут.

    Отличается от TabPayAPIError принципиально: результат запроса НЕИЗВЕСТЕН,
    платёж мог создаться на стороне TabPay. Повторять вслепую нельзя — сначала
    сверка по своему orderId.
    """


class TabPayService:
    """Клиент REST API TabPay.

    Аутентификация — заголовок ``X-Api-Key`` (ключ магазина ``tp_...``).
    Вебхуки подписываются отдельным «секретом подписи» магазина.
    """

    # Границы, зашитые в API создания платежа: 1₽ и 100 млн ₽.
    API_MIN_AMOUNT_KOPEKS = 100
    API_MAX_AMOUNT_KOPEKS = 10_000_000_000

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    @property
    def base_url(self) -> str:
        return (settings.TABPAY_BASE_URL or 'https://tabpay.org/api').rstrip('/')

    @property
    def api_key(self) -> str:
        return settings.TABPAY_API_KEY or ''

    @property
    def webhook_secret(self) -> str:
        return settings.TABPAY_WEBHOOK_SECRET or ''

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

    def _headers(self) -> dict[str, str]:
        return {
            'X-Api-Key': self.api_key,
            'Content-Type': 'application/json',
        }

    @staticmethod
    def _error_message(data: Any) -> str:
        """Достаёт текст из единого формата ошибки TabPay.

        При ошибках валидации ``message`` — массив со всеми проблемами сразу.
        """
        if isinstance(data, dict):
            message = data.get('message')
            if isinstance(message, list):
                return '; '.join(str(item) for item in message)
            if message:
                return str(message)
            if data.get('error'):
                return str(data['error'])
        return str(data)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        """Выполняет запрос к API.

        ``allow_404=True`` превращает «не найдено» в ``None`` — это штатный ответ
        поиска по orderId, а не ошибка.
        """
        url = f'{self.base_url}/{path.lstrip("/")}'
        try:
            session = await self._get_session()
            async with session.request(
                method,
                url,
                json=json_payload,
                params=params,
                headers=self._headers(),
            ) as response:
                data = await response.json(content_type=None)

                if response.status == 404 and allow_404:
                    return None

                if response.status >= 400:
                    message = self._error_message(data)
                    logger.error('TabPay API error', url=url, status=response.status, message=message)
                    raise TabPayAPIError(response.status, message)

                return data if isinstance(data, dict) else {'_raw': data}
        except (aiohttp.ClientError, TimeoutError) as error:
            # Ответа нет — исход запроса неизвестен. Решение о повторе принимает
            # вызывающий, предварительно сверившись по orderId.
            logger.error('TabPay API connection error', url=url, error=str(error))
            raise TabPayNetworkError(str(error)) from error

    async def create_payment(
        self,
        *,
        order_id: str,
        amount_kopecks: int,
        description: str | None = None,
        email: str | None = None,
        telegram_id: int | str | None = None,
        metadata: dict[str, Any] | None = None,
        success_url: str | None = None,
        fail_url: str | None = None,
        method: str | None = None,
    ) -> dict[str, Any]:
        """POST /v1/payments — создаёт платёж и возвращает объект с payUrl.

        Дубликат orderId — 409, недоступный магазину способ оплаты — тоже 409.
        """
        payload: dict[str, Any] = {
            'orderId': order_id[:64],
            'amountKopecks': int(amount_kopecks),
        }
        if description:
            payload['description'] = description[:255]
        if email:
            payload['email'] = email
        if telegram_id is not None:
            payload['telegramId'] = str(telegram_id)
        if metadata:
            payload['metadata'] = metadata
        if success_url:
            payload['successUrl'] = success_url
        if fail_url:
            payload['failUrl'] = fail_url
        if method:
            payload['method'] = method

        logger.info('TabPay create_payment', order_id=order_id, amount_kopecks=amount_kopecks, method=method)

        data = await self._request('POST', '/v1/payments', json_payload=payload)
        if not data or not data.get('id') or not data.get('payUrl'):
            # Без id и payUrl платёж бесполезен: покупателя некуда вести, а
            # пришедший позже вебхук не с чем сопоставить.
            logger.error('TabPay create_payment: неполный ответ', order_id=order_id, response_data=data)
            raise TabPayAPIError(200, f'Incomplete create payment response: {data}')

        logger.info(
            'TabPay payment created',
            order_id=order_id,
            payment_id=data.get('id'),
            status=data.get('status'),
            is_test=bool(data.get('isTest')),
        )
        return data

    async def get_payment(self, payment_id: str) -> dict[str, Any] | None:
        """GET /v1/payments/{id} — платёж по идентификатору TabPay (None, если нет)."""
        return await self._request('GET', f'/v1/payments/{payment_id}', allow_404=True)

    async def get_payment_by_order_id(self, order_id: str) -> dict[str, Any] | None:
        """GET /v1/payments?orderId=... — платёж по нашему номеру заказа (None, если нет)."""
        return await self._request('GET', '/v1/payments', params={'orderId': order_id}, allow_404=True)

    async def create_payment_reconciled(self, *, order_id: str, **kwargs: Any) -> dict[str, Any]:
        """Создаёт платёж, безопасно переживая сетевые сбои и дубли orderId.

        Ответ на создание может потеряться уже после того, как TabPay записал
        платёж. Повторное создание вслепую либо получит 409, либо (при другом
        orderId) заведёт второй счёт на ту же покупку. Поэтому при неизвестном
        исходе сначала сверяемся по своему orderId и переиспользуем найденный
        платёж вместе с его payUrl.
        """
        try:
            return await self.create_payment(order_id=order_id, **kwargs)
        except TabPayAPIError as error:
            if error.status_code == 409:
                # Дубликат orderId: платёж уже есть — берём существующий.
                existing = await self.get_payment_by_order_id(order_id)
                if existing:
                    logger.info('TabPay: orderId уже занят, используем существующий платёж', order_id=order_id)
                    return existing
                # 409 без находки означает другую причину (способ оплаты
                # недоступен магазину) — её повторами не вылечить.
                raise
            if error.status_code < 500:
                raise
            logger.warning('TabPay: 5xx на создании платежа, сверяемся по orderId', order_id=order_id)
        except TabPayNetworkError:
            logger.warning('TabPay: ответ на создание не получен, сверяемся по orderId', order_id=order_id)

        return await self._recreate_after_unknown_outcome(order_id=order_id, **kwargs)

    async def _recreate_after_unknown_outcome(self, *, order_id: str, **kwargs: Any) -> dict[str, Any]:
        """Сверка по orderId и, если платежа нет, единственный повтор создания."""
        existing = await self.get_payment_by_order_id(order_id)
        if existing:
            logger.info('TabPay: платёж всё-таки создался, повтор не нужен', order_id=order_id)
            return existing

        try:
            return await self.create_payment(order_id=order_id, **kwargs)
        except (TabPayAPIError, TabPayNetworkError):
            # Последняя попытка разобраться: повтор мог снова потерять ответ.
            recovered = await self.get_payment_by_order_id(order_id)
            if recovered:
                logger.info('TabPay: платёж найден после неудачного повтора', order_id=order_id)
                return recovered
            raise

    def verify_webhook_signature(
        self,
        raw_body: bytes,
        timestamp: str | None,
        signature_v2: str | None,
        *,
        max_age_seconds: int | None = None,
    ) -> bool:
        """Проверяет X-Signature-V2 по СЫРЫМ байтам тела.

        Подпись — HMAC-SHA256 от «{X-Timestamp}.{тело}» ключом «секрет подписи»
        магазина. Тело берётся до разбора JSON: пересобранный JSON меняет порядок
        ключей и пробелы, и подпись перестаёт сходиться. Метка проверяется на
        свежесть в обе стороны — иначе перехваченный вебхук можно переиграть.
        """
        try:
            received = (signature_v2 or '').strip()
            if not received:
                logger.warning('TabPay webhook: отсутствует X-Signature-V2')
                return False

            secret = self.webhook_secret
            if not secret:
                # С пустым секретом HMAC считался бы от известного ключа —
                # подпись подделал бы кто угодно. Отказываем.
                logger.error('TabPay webhook: не задан секрет подписи, проверка невозможна')
                return False

            raw_timestamp = (timestamp or '').strip()
            if not raw_timestamp.isdigit():
                logger.warning('TabPay webhook: некорректный X-Timestamp', timestamp=raw_timestamp[:32])
                return False

            window = max_age_seconds if max_age_seconds is not None else settings.TABPAY_WEBHOOK_MAX_AGE_SECONDS
            age = abs(time.time() - int(raw_timestamp))
            if age > window:
                logger.warning(
                    'TabPay webhook: метка времени вне окна допуска (проверьте синхронизацию часов по NTP)',
                    age_seconds=int(age),
                    window_seconds=window,
                )
                return False

            payload = raw_timestamp.encode('utf-8') + b'.' + raw_body
            expected = hmac.new(secret.encode('utf-8'), payload, hashlib.sha256).hexdigest()

            if not hmac.compare_digest(expected, received.lower()):
                logger.warning('TabPay webhook: invalid signature', received_prefix=received[:8])
                return False
            return True
        except Exception as error:
            logger.error('TabPay webhook verify error', error=str(error))
            return False


# Singleton instance
tabpay_service = TabPayService()
