"""Сервис для работы с API Cashera (api.cashera.cash, server-to-server)."""

import asyncio
import hmac
from typing import Any
from urllib.parse import quote

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

# Повторяем только то, что по документации безопасно повторять с тем же external_id:
# сетевые сбои, 429 и 5xx. 4xx (кроме 429) повтором не лечится.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_BASE_DELAY_SECONDS = 1.0
_MAX_RETRY_AFTER_SECONDS = 10.0


class CasheraAPIError(Exception):
    """Ошибка API Cashera."""

    def __init__(self, status_code: int, message: str, errors: Any = None) -> None:
        self.status_code = status_code
        self.message = message
        self.errors = errors
        super().__init__(f'Cashera API error ({status_code}): {message}')


def normalize_payment_url(url: str | None) -> str | None:
    """В примерах Cashera ``payment_url`` приходит без схемы (``pay.cashera.cash/...``).

    Telegram не примет такую ссылку в кнопке, браузер откроет её как относительную —
    дописываем ``https://``.
    """
    if not url:
        return None
    url = str(url).strip()
    if not url:
        return None
    if url.startswith(('https://', 'http://')):
        return url
    return f'https://{url.lstrip("/")}'


class CasheraService:
    """Клиент Cashera Integration API.

    Аутентификация — заголовок X-Api-Key. Суммы — целые числа в минорных единицах
    (для RUB — копейки). Вебхук подтверждается заголовками X-Api-Key и X-Secret,
    которые сверяются с сохранёнными значениями в постоянном времени.
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    @property
    def base_url(self) -> str:
        return (settings.CASHERA_BASE_URL or 'https://api.cashera.cash/api/v1').rstrip('/')

    @property
    def api_key(self) -> str:
        return settings.CASHERA_API_KEY or ''

    @property
    def api_secret(self) -> str:
        return settings.CASHERA_API_SECRET or ''

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _headers(self) -> dict[str, str]:
        return {
            'X-Api-Key': self.api_key,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), _MAX_RETRY_AFTER_SECONDS)
            except ValueError:
                pass
        return _BASE_DELAY_SECONDS * (2 ** (attempt - 1))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f'{self.base_url}/{path.lstrip("/")}'
        last_error: Exception | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                session = await self._get_session()
                async with session.request(method, url, json=json_payload, headers=self._headers()) as response:
                    try:
                        data = await response.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        data = {'message': (await response.text())[:500]}

                    if response.status < 400:
                        return data if isinstance(data, dict) else {'_raw': data}

                    message = data.get('message') if isinstance(data, dict) else str(data)
                    errors = data.get('errors') if isinstance(data, dict) else None
                    error = CasheraAPIError(response.status, str(message or ''), errors)

                    if response.status in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
                        delay = self._retry_delay(attempt, response.headers.get('Retry-After'))
                        logger.warning(
                            'Cashera API: временная ошибка, повтор',
                            url=url,
                            status=response.status,
                            attempt=attempt,
                            delay=delay,
                        )
                        last_error = error
                        await asyncio.sleep(delay)
                        continue

                    logger.error(
                        'Cashera API error',
                        url=url,
                        status=response.status,
                        message=message,
                        errors=errors,
                    )
                    raise error
            except (aiohttp.ClientError, TimeoutError) as error:
                last_error = error
                if attempt < _MAX_ATTEMPTS:
                    delay = self._retry_delay(attempt, None)
                    logger.warning(
                        'Cashera API: сетевая ошибка, повтор',
                        url=url,
                        attempt=attempt,
                        delay=delay,
                        error=str(error),
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error('Cashera API connection error', url=url, error=str(error))
                raise

        # Сюда попадаем, только если последняя попытка была повторяемой ошибкой.
        assert last_error is not None
        raise last_error

    async def create_transaction(
        self,
        *,
        amount_kopeks: int,
        external_id: str,
        description: str,
        payment_method: str | None,
        callback_url: str | None = None,
        success_url: str | None = None,
        fail_url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Создаёт платёж: POST /integration/transactions.

        Ответ 201 — объект транзакции (uuid, status=pending, payment_url, expires_at).
        Повтор с тем же external_id и тем же телом возвращает ту же транзакцию.
        """
        payload: dict[str, Any] = {
            'amount': int(amount_kopeks),
            'currency': 'RUB',
            'external_id': external_id,
            'description': (description or 'Пополнение баланса')[:255],
        }
        if payment_method:
            payload['payment_method'] = payment_method
        if callback_url:
            payload['callback_url'] = callback_url
        if success_url:
            payload['success_url'] = success_url
        if fail_url:
            payload['fail_url'] = fail_url
        if metadata:
            payload['metadata'] = metadata

        logger.info(
            'Cashera API create_transaction',
            external_id=external_id,
            amount_kopeks=amount_kopeks,
            payment_method=payment_method,
        )

        data = await self._request('POST', '/integration/transactions', json_payload=payload)
        if not data.get('uuid'):
            logger.error('Cashera create_transaction: в ответе нет uuid', external_id=external_id)
            raise CasheraAPIError(201, 'Incomplete create transaction response')
        return data

    async def get_transaction(self, transaction_uuid: str) -> dict[str, Any]:
        """Текущее состояние транзакции: GET /integration/transactions/{uuid}."""
        return await self._request('GET', f'/integration/transactions/{quote(str(transaction_uuid), safe="")}')

    async def get_transaction_by_external_id(self, external_id: str) -> dict[str, Any]:
        """Транзакция по нашему id: GET /integration/transactions/by-external-id/{external_id}."""
        return await self._request(
            'GET', f'/integration/transactions/by-external-id/{quote(str(external_id), safe="")}'
        )

    async def get_h2h(self, transaction_uuid: str) -> dict[str, Any]:
        """Реквизиты для своего экрана оплаты: GET /integration/transactions/{uuid}/h2h.

        Ответ: ``amount`` и ``qr`` (строка QR СБП или платёжная ссылка). Пока провайдер
        не присвоил платежу идентификатор, отвечает 422 — вызывающий повторяет.
        """
        return await self._request('GET', f'/integration/transactions/{quote(str(transaction_uuid), safe="")}/h2h')

    # --- подписки (автопродление, sbp_recurring) ---------------------------------

    async def create_subscription(
        self,
        *,
        amount_kopeks: int,
        external_id: str,
        interval: str,
        description: str,
        callback_url: str | None = None,
    ) -> dict[str, Any]:
        """POST /integration/subscriptions — подписка ждёт подтверждения по payment_url.

        Повтор с тем же external_id и теми же параметрами возвращает существующую
        подписку (200); другие параметры при том же external_id — 409.
        """
        payload: dict[str, Any] = {
            'amount': int(amount_kopeks),
            'external_id': external_id,
            'interval': interval,
            'description': (description or 'Подписка')[:255],
        }
        if callback_url:
            payload['callback_url'] = callback_url
        logger.info(
            'Cashera API create_subscription', external_id=external_id, amount_kopeks=amount_kopeks, interval=interval
        )
        data = await self._request('POST', '/integration/subscriptions', json_payload=payload)
        if not data.get('uuid'):
            logger.error('Cashera create_subscription: в ответе нет uuid', external_id=external_id)
            raise CasheraAPIError(201, 'Incomplete create subscription response')
        return data

    async def get_subscription(self, subscription_uuid: str) -> dict[str, Any]:
        return await self._request('GET', f'/integration/subscriptions/{quote(str(subscription_uuid), safe="")}')

    async def get_subscription_by_external_id(self, external_id: str) -> dict[str, Any]:
        return await self._request(
            'GET', f'/integration/subscriptions/by-external-id/{quote(str(external_id), safe="")}'
        )

    async def list_subscription_charges(
        self, subscription_uuid: str, *, page: int = 1, per_page: int = 100
    ) -> list[dict[str, Any]]:
        """История списаний по подписке (постранично). Возвращает список транзакций."""
        data = await self._request(
            'GET',
            f'/integration/subscriptions/{quote(str(subscription_uuid), safe="")}/charges?page={int(page)}&per_page={int(per_page)}',
        )
        for key in ('data', 'items', 'charges', 'transactions'):
            if isinstance(data.get(key), list):
                return [item for item in data[key] if isinstance(item, dict)]
        raw = data.get('_raw')
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    async def cancel_subscription(self, subscription_uuid: str) -> dict[str, Any]:
        """POST /integration/subscriptions/{uuid}/cancel — идемпотентна для уже отменённой."""
        return await self._request(
            'POST', f'/integration/subscriptions/{quote(str(subscription_uuid), safe="")}/cancel'
        )

    def verify_webhook(self, api_key_header: str | None, secret_header: str | None) -> bool:
        """Сверяет X-Api-Key и X-Secret вебхука с нашими в постоянном времени.

        Секрет никогда не логируется. Без настроенных ключа и секрета вебхук не
        принимается вовсе: сравнение с пустой строкой подделал бы кто угодно.
        """
        if not self.api_key or not self.api_secret:
            logger.error('Cashera webhook: не заданы CASHERA_API_KEY/CASHERA_API_SECRET, вебхук отклонён')
            return False
        key_ok = hmac.compare_digest((api_key_header or '').encode(), self.api_key.encode())
        secret_ok = hmac.compare_digest((secret_header or '').encode(), self.api_secret.encode())
        if not (key_ok and secret_ok):
            logger.warning('Cashera webhook: неверные учётные данные', key_ok=key_ok, secret_ok=secret_ok)
            return False
        return True


# Singleton instance
cashera_service = CasheraService()
