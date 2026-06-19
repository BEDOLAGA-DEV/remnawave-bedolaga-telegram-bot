"""Сервис для работы с API Lava Business (api.lava.ru).

Контракт (актуальные доки https://dev.lava.ru/business-objects-invoice):

* Базовый URL: ``https://api.lava.ru``.
* Эндпоинты — ``/business/invoice/create``, ``/business/invoice/status``,
  ``/business/invoice/get-available-tariffs``.
* Подпись исходящих запросов: ``HMAC-SHA256(raw_body_bytes, shop_secret_key)`` → hex,
  передаётся в HTTP-заголовке ``Signature``. Подписываем те же самые байты, что
  уходят в запрос — никаких пересортировок, иначе подпись разойдётся.
* Webhook от Lava: HMAC-SHA256 (тот же ключ — ``additional_key``) в заголовке
  ``Authorization``. Lava на разных шопах подписывает то raw body, то
  пере-сериализованный JSON со sorted-keys, поэтому верификация толерантна:
  принимаем подпись, если совпала хотя бы с одной из двух канонизаций.

История: до коммита 2026-05-16 подпись клалась в body-поле ``signature``
(под старый PHP SDK). Новые роуты `/business/*` это **не** принимают — Lava
отвечает 401 с `Invalid signature`. Если вернёшься к body-signature —
сломаешь интеграцию: см. тест ``tests/services/test_lava_service.py``.
"""

import hashlib
import hmac
import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


def _strip_url_query(url: str) -> str:
    """Return ``url`` without its query string or fragment.

    Lava Business rejects successUrl/failUrl that carry a query string ("ошибочный формат
    ссылки", HTTP 422). Callers should pass clean URLs, but strip defensively so a stray
    ``?param=`` (e.g. a misconfigured LAVA_RETURN_URL) can't break invoice creation.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


class LavaAPIError(Exception):
    """Ошибка API Lava."""

    def __init__(self, status_code: int, message: str, code: str | int | None = None) -> None:
        self.status_code = status_code
        self.message = message
        self.api_code = code
        super().__init__(f'Lava API error ({status_code}): {message}')


class LavaService:
    """Клиент для Lava Business API (api.lava.ru).

    Ключи:
    * ``LAVA_SECRET_KEY`` — shop_secret_key, подписывает исходящие запросы.
    * ``LAVA_WEBHOOK_SECRET`` — shop_webhook_additional_key, подписывает входящие webhook'и.
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    @property
    def base_url(self) -> str:
        return (settings.LAVA_BASE_URL or 'https://api.lava.ru').rstrip('/')

    @property
    def shop_id(self) -> str:
        return settings.LAVA_SHOP_ID or ''

    @property
    def secret_key(self) -> str:
        return settings.LAVA_SECRET_KEY or ''

    @property
    def webhook_secret(self) -> str:
        return settings.LAVA_WEBHOOK_SECRET or ''

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

    @staticmethod
    def _canonical_json(payload: dict[str, Any]) -> str:
        """JSON со sort_keys=True (эквивалент php ``ksort + json_encode``).

        Используется только в webhook-верификации как fallback на случай, если
        конкретный shop Lava подписывает не raw body, а пере-сериализованный JSON.
        Исходящие запросы канонизацию НЕ используют — подписывается raw body.

        Поле ``signature`` исключается из канонизации, ``float n.0`` приводится
        к ``int`` (php-совместимо).
        """

        def normalize(value: Any) -> Any:
            if isinstance(value, float) and value.is_integer():
                return int(value)
            if isinstance(value, dict):
                return {key: normalize(item) for key, item in value.items() if key != 'signature'}
            if isinstance(value, list):
                return [normalize(item) for item in value]
            return value

        without_sig = {key: normalize(value) for key, value in payload.items() if key != 'signature'}
        return json.dumps(without_sig, sort_keys=True, separators=(',', ':'))

    def _hmac_hex(self, message: str | bytes, key: str | None = None) -> str:
        secret = (key if key is not None else self.secret_key) or ''
        msg_bytes = message if isinstance(message, (bytes, bytearray)) else message.encode('utf-8')
        return hmac.new(
            secret.encode('utf-8'),
            msg=msg_bytes,
            digestmod=hashlib.sha256,
        ).hexdigest()

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST в Lava с подписью в HTTP-заголовке ``Signature``.

        Подписывается БАЙТ-В-БАЙТ то тело, которое уходит в запрос —
        никаких пересортировок, порядок ключей сохраняется как в payload.
        """
        url = f'{self.base_url}/{path.lstrip("/")}'
        body = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
        body_bytes = body.encode('utf-8')
        signature = self._hmac_hex(body_bytes)
        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Signature': signature,
        }
        try:
            session = await self._get_session()
            async with session.post(url, data=body_bytes, headers=headers) as response:
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    text = await response.text()
                    data = {'_raw': text}
                if not isinstance(data, dict):
                    data = {'_raw': data}
                if response.status >= 400:
                    error_msg = (
                        data.get('error')
                        or (data.get('data') or {}).get('error')
                        or data.get('message')
                        or 'Lava API HTTP error'
                    )
                    logger.warning(
                        'Lava API HTTP error',
                        url=url,
                        status=response.status,
                        error_msg=str(error_msg),
                        code=data.get('code'),
                    )
                    raise LavaAPIError(response.status, str(error_msg), data.get('code'))
                return data
        except aiohttp.ClientError as error:
            logger.exception('Lava API connection error', url=url, error=error)
            raise

    async def create_invoice(
        self,
        *,
        amount_rubles: float,
        order_id: str,
        success_url: str | None = None,
        fail_url: str | None = None,
        hook_url: str | None = None,
        expire_minutes: int | None = None,
        comment: str | None = None,
        custom_fields: str | None = None,
        include_service: list[str] | None = None,
        exclude_service: list[str] | None = None,
    ) -> dict[str, Any]:
        """Создаёт инвойс через POST /business/invoice/create.

        Сумма передаётся в рублях с двумя знаками после запятой.
        ``orderId`` — наш уникальный идентификатор платежа.
        """
        payload: dict[str, Any] = {
            'sum': round(float(amount_rubles), 2),
            'orderId': str(order_id),
            'shopId': self.shop_id,
        }
        if hook_url:
            payload['hookUrl'] = hook_url[:500]
        if success_url:
            payload['successUrl'] = _strip_url_query(success_url)[:500]
        if fail_url:
            payload['failUrl'] = _strip_url_query(fail_url)[:500]
        if expire_minutes is not None:
            # Lava лимит: 1..7200 минут (5 дней)
            payload['expire'] = max(1, min(7200, int(expire_minutes)))
        if comment:
            payload['comment'] = comment[:255]
        if custom_fields:
            payload['customFields'] = custom_fields[:500]
        if include_service:
            payload['includeService'] = list(include_service)
        if exclude_service:
            payload['excludeService'] = list(exclude_service)

        logger.info('Lava API invoice/create', order_id=order_id, sum=payload['sum'])
        data = await self._post('/business/invoice/create', payload)

        # Lava возвращает {"status": "success", "data": {...}} или {"status": "error", "error": "..."}
        if isinstance(data.get('status'), str) and data['status'].lower() == 'error':
            raise LavaAPIError(200, str(data.get('error') or data.get('message') or 'unknown'))

        return data

    async def get_invoice_status(
        self,
        *,
        order_id: str | None = None,
        invoice_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /business/invoice/status — статус инвойса по orderId или invoiceId."""
        if not order_id and not invoice_id:
            raise ValueError('Lava status: order_id or invoice_id required')

        payload: dict[str, Any] = {'shopId': self.shop_id}
        if invoice_id:
            payload['invoiceId'] = str(invoice_id)
        if order_id:
            payload['orderId'] = str(order_id)

        logger.info('Lava API invoice/status', order_id=order_id, invoice_id=invoice_id)
        return await self._post('/business/invoice/status', payload)

    async def get_services(self) -> dict[str, Any]:
        """POST /business/invoice/get-available-tariffs — доступные методы оплаты для shopId."""
        payload: dict[str, Any] = {'shopId': self.shop_id}
        return await self._post('/business/invoice/get-available-tariffs', payload)

    def verify_webhook_signature(self, raw_body: bytes, received_signature: str) -> bool:
        """Верификация подписи webhook'а.

        Lava на разных shop'ах подписывает webhook'и по-разному:
          1. HMAC от raw body (актуальный контракт api.lava.ru).
          2. HMAC от пере-сериализованного JSON с sorted-keys (legacy PHP SDK).
        Пробуем оба варианта, принимаем при совпадении хотя бы одного.
        Поле подписи приходит в HTTP-заголовке ``Authorization`` (webserver его
        парсит и передаёт сюда уже строкой).
        """
        try:
            if not received_signature:
                logger.warning('Lava webhook: отсутствует заголовок подписи')
                return False
            if not self.webhook_secret:
                logger.error('Lava webhook: LAVA_WEBHOOK_SECRET не настроен')
                return False

            received = received_signature.strip()

            # Вариант 1: HMAC от raw body — основной контракт.
            expected_raw = self._hmac_hex(raw_body, key=self.webhook_secret)
            if hmac.compare_digest(expected_raw.lower(), received.lower()):
                return True

            # Вариант 2: HMAC от canonical JSON (sort_keys) — legacy PHP SDK shops.
            expected_canonical: str | None = None
            try:
                payload = json.loads(raw_body)
                if isinstance(payload, dict):
                    canonical = self._canonical_json(payload)
                    expected_canonical = self._hmac_hex(canonical, key=self.webhook_secret)
                    if hmac.compare_digest(expected_canonical.lower(), received.lower()):
                        return True
            except (ValueError, TypeError) as parse_error:
                logger.warning(
                    'Lava webhook: невалидный JSON для альтернативной проверки',
                    error=str(parse_error),
                    received_prefix=received[:8],
                    expected_raw_prefix=expected_raw[:8],
                )
                return False

            logger.warning(
                'Lava webhook: invalid signature',
                received_prefix=received[:8],
                expected_raw_prefix=expected_raw[:8],
                expected_canonical_prefix=expected_canonical[:8] if expected_canonical else None,
            )
            return False
        except Exception as error:
            logger.error('Lava webhook verify error', error=str(error), exc_info=True)
            return False


# Singleton instance
lava_service = LavaService()
