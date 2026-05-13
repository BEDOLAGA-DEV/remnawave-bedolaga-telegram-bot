"""Сервис для работы с API Lava Business (gate.lava.ru)."""

import hashlib
import hmac
import json
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class LavaAPIError(Exception):
    """Ошибка API Lava."""

    def __init__(self, status_code: int, message: str, code: str | int | None = None) -> None:
        self.status_code = status_code
        self.message = message
        self.api_code = code
        super().__init__(f'Lava API error ({status_code}): {message}')


class LavaService:
    """Клиент для Lava Business API (gate.lava.ru).

    Подпись запросов: HMAC-SHA256(json_body, secret_key) → hex.
    Передаётся в заголовке ``Signature``.
    Ключи `secret_key` (запросы) и `secret_key_2` (webhook) выдаются мерчанту в личном кабинете.
    Каноническая строка для подписи — JSON в том же порядке, в котором отправляется в теле.
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    @property
    def base_url(self) -> str:
        return (settings.LAVA_BASE_URL or 'https://gate.lava.ru').rstrip('/')

    @property
    def shop_id(self) -> str:
        return settings.LAVA_SHOP_ID or ''

    @property
    def secret_key(self) -> str:
        return settings.LAVA_SECRET_KEY or ''

    @property
    def webhook_secret(self) -> str:
        # secret_key_2 — для проверки подписи webhook'а
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
    def _serialize(payload: dict[str, Any]) -> str:
        """Сериализация JSON для подписи и тела запроса.

        Lava подписывает байт-в-байт ту же строку, что и отправляется в теле, поэтому
        порядок ключей определяется порядком вставки в payload (Python ≥3.7 dict сохраняет
        порядок). Используем компактный сепаратор и UTF-8 без экранирования юникода.
        """
        return json.dumps(payload, separators=(',', ':'), ensure_ascii=False)

    def _hmac_hex(self, message: str | bytes, key: str | None = None) -> str:
        secret = (key if key is not None else self.secret_key) or ''
        msg_bytes = message if isinstance(message, (bytes, bytearray)) else message.encode('utf-8')
        return hmac.new(
            secret.encode('utf-8'),
            msg=msg_bytes,
            digestmod=hashlib.sha256,
        ).hexdigest()

    def _build_headers(self, body: str) -> dict[str, str]:
        return {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Signature': self._hmac_hex(body),
        }

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f'{self.base_url}/{path.lstrip("/")}'
        body = self._serialize(payload)
        try:
            session = await self._get_session()
            async with session.post(url, data=body, headers=self._build_headers(body)) as response:
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
        """Создаёт инвойс через POST /api/v2/invoice/create.

        Сумма передаётся в рублях с двумя знаками после запятой.
        ``orderId`` — наш уникальный идентификатор платежа.
        """
        # Порядок полей важен (этим же порядком сериализуется и подписывается)
        payload: dict[str, Any] = {
            'sum': round(float(amount_rubles), 2),
            'orderId': str(order_id),
            'shopId': self.shop_id,
        }
        if hook_url:
            payload['hookUrl'] = hook_url[:500]
        if success_url:
            payload['successUrl'] = success_url[:500]
        if fail_url:
            payload['failUrl'] = fail_url[:500]
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
        data = await self._post('/api/v2/invoice/create', payload)

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
        """POST /api/v2/invoice/status — статус инвойса по orderId или invoiceId."""
        if not order_id and not invoice_id:
            raise ValueError('Lava status: order_id or invoice_id required')

        payload: dict[str, Any] = {'shopId': self.shop_id}
        if invoice_id:
            payload['invoiceId'] = str(invoice_id)
        if order_id:
            payload['orderId'] = str(order_id)

        logger.info('Lava API invoice/status', order_id=order_id, invoice_id=invoice_id)
        return await self._post('/api/v2/invoice/status', payload)

    async def get_services(self) -> dict[str, Any]:
        """POST /api/v2/invoice/services — доступные методы оплаты для shopId."""
        payload: dict[str, Any] = {'shopId': self.shop_id}
        return await self._post('/api/v2/invoice/services', payload)

    def verify_webhook_signature(self, raw_body: bytes, received_signature: str) -> bool:
        """Верификация подписи webhook (заголовок ``Authorization``).

        Lava Business webhook подписан HMAC-SHA256 от raw JSON body ключом ``secret_key_2``.
        """
        try:
            if not received_signature:
                logger.warning('Lava webhook: отсутствует Authorization header')
                return False
            if not self.webhook_secret:
                logger.error('Lava webhook: LAVA_WEBHOOK_SECRET не настроен')
                return False

            # HMAC берётся напрямую от raw bytes — без decode/encode round-trip,
            # чтобы не терять байты при некорректной кодировке payload.
            expected = self._hmac_hex(raw_body, key=self.webhook_secret)
            received = received_signature.strip()

            if not hmac.compare_digest(expected.lower(), received.lower()):
                logger.warning(
                    'Lava webhook: invalid signature',
                    received_prefix=received[:8],
                )
                return False
            return True
        except Exception as error:
            logger.error('Lava webhook verify error', error=error)
            return False


# Singleton instance
lava_service = LavaService()
