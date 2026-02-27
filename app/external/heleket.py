"""HTTP client for Heleket payment API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

import aiohttp
import structlog

from app.config import settings
from app.localization.texts import get_texts


logger = structlog.get_logger(__name__)
texts = get_texts('ru')


class HeleketService:
    """Minimal wrapper around Heleket API endpoints."""

    def __init__(self) -> None:
        self.base_url = settings.HELEKET_BASE_URL.rstrip('/')
        self.merchant_id = settings.HELEKET_MERCHANT_ID
        self.api_key = settings.HELEKET_API_KEY

    @property
    def is_configured(self) -> bool:
        return bool(self.merchant_id and self.api_key)

    def _prepare_body(
        self,
        payload: dict[str, Any],
        *,
        ignore_none: bool,
        sort_keys: bool,
    ) -> str:
        if ignore_none:
            cleaned = {key: value for key, value in payload.items() if value is not None}
        else:
            cleaned = dict(payload)

        serialized = json.dumps(
            cleaned,
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=sort_keys,
        )

        if '/' in serialized:
            serialized = serialized.replace('/', '\\/')

        return serialized

    def _generate_signature(self, body: str) -> str:
        api_key = self.api_key or ''
        encoded = base64.b64encode(body.encode('utf-8')).decode('utf-8')
        raw = f'{encoded}{api_key}'
        return hashlib.md5(raw.encode('utf-8')).hexdigest()

    async def _request(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not self.is_configured:
            logger.error('Heleket сервис не настроен: merchant или api_key отсутствуют')
            return None

        body = self._prepare_body(payload, ignore_none=True, sort_keys=True)
        signature = self._generate_signature(body)

        url = f'{self.base_url}/{endpoint.lstrip("/")}'
        headers = {
            'merchant': self.merchant_id or '',
            'sign': signature,
            'Content-Type': 'application/json',
        }

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.post(
                    url,
                    data=body.encode('utf-8'),
                    headers=headers,
                    params=params,
                ) as response,
            ):
                text = await response.text()
                if response.content_type != 'application/json':
                    logger.error('Ответ Heleket не JSON', content_type=response.content_type, text=text)
                    return None

                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.error('Ошибка парсинга Heleket JSON', text=text)
                    return None

                if response.status >= 400:
                    logger.error(
                        'Heleket API вернул статус', endpoint=endpoint, response_status=response.status, data=data
                    )
                    return None

                if isinstance(data, dict) and data.get('state') == 0:
                    return data

                logger.error('Heleket API вернул ошибку', data=data)
                return None
        except Exception as error:  # pragma: no cover - defensive
            logger.error('Ошибка запроса к Heleket API', error=error)
            return None

    async def create_payment(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        return await self._request('payment', payload)

    async def get_payment_info(
        self,
        *,
        uuid: str | None = None,
        order_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not uuid and not order_id:
            raise ValueError(
                texts.t(
                    'HELEKET_PAYMENT_INFO_MISSING_IDENTIFIERS',
                    'Нужно указать uuid или order_id для Heleket payment/info',
                )
            )

        payload: dict[str, Any] = {}
        if uuid:
            payload['uuid'] = uuid
        if order_id:
            payload['order_id'] = order_id

        return await self._request('payment/info', payload)

    async def list_payments(
        self,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {}
        if date_from:
            payload['date_from'] = date_from
        if date_to:
            payload['date_to'] = date_to

        params = {'cursor': cursor} if cursor else None
        return await self._request('payment/list', payload, params=params)

    def verify_webhook_signature(self, payload: dict[str, Any]) -> bool:
        if not self.is_configured:
            logger.warning('Heleket сервис не настроен, подпись пропускается')
            return True

        if not isinstance(payload, dict):
            logger.error('Heleket webhook payload не dict', payload=payload)
            return False

        signature = payload.get('sign')
        if not signature:
            logger.error('Heleket webhook без подписи')
            return False

        data = dict(payload)
        data.pop('sign', None)
        body = self._prepare_body(data, ignore_none=False, sort_keys=False)
        expected = self._generate_signature(body)

        is_valid = hmac.compare_digest(expected, str(signature))

        if not is_valid:
            logger.error(
                'Неверная подпись Heleket webhook: ожидается , получено', expected=expected, signature=signature
            )
        return is_valid
