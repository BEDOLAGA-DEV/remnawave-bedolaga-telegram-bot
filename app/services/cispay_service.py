"""Сервис для работы с API cisPay (H2H merchant API, api.cispay.app)."""

import hashlib
import hmac
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class CisPayAPIError(Exception):
    """Ошибка API cisPay."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f'cisPay API error ({status_code}): {message}')


class CisPayService:
    """Клиент для cisPay Merchant API (api.cispay.app).

    Аутентификация — заголовки X-Shop-ID (UUID магазина) и X-Api-Key (cis_sec_...).
    Суммы везде в копейках. Вебхук подписывается HMAC-SHA256 от сырого тела
    запроса ключом X-Api-Key (заголовок X-Signature).
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    @property
    def base_url(self) -> str:
        return (settings.CISPAY_BASE_URL or 'https://api.cispay.app').rstrip('/')

    @property
    def shop_id(self) -> str:
        return settings.CISPAY_SHOP_ID or ''

    @property
    def api_key(self) -> str:
        return settings.CISPAY_API_KEY or ''

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
            'X-Shop-ID': self.shop_id,
            'X-Api-Key': self.api_key,
            'Content-Type': 'application/json',
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
                if response.status >= 400:
                    detail = data.get('detail') if isinstance(data, dict) else data
                    logger.error(
                        'cisPay API error',
                        url=url,
                        status=response.status,
                        detail=detail,
                    )
                    raise CisPayAPIError(response.status, str(detail))
                return data if isinstance(data, dict) else {'_raw': data}
        except aiohttp.ClientError as error:
            logger.exception('cisPay API connection error', url=url, error=error)
            raise

    async def create_payment(
        self,
        *,
        amount_kopeks: int,
        order_id: str,
        payment_method: str,
        customer_id: str,
        description: str | None = None,
        redirect_success_url: str | None = None,
        redirect_fail_url: str | None = None,
    ) -> dict[str, Any]:
        """Создаёт платёж (счёт) в cisPay.

        POST /payments — возвращает id (UUIDv7), status=PENDING, payment_url
        хостинговой страницы оплаты (карта/3-DS/QR СБП на стороне cisPay).
        customer_id обязателен для SBP — провайдер СБП отклоняет платежи
        без идентификатора плательщика.
        """
        payload: dict[str, Any] = {
            'amount': int(amount_kopeks),
            'currency': (settings.CISPAY_CURRENCY or 'RUB').upper(),
            'order_id': order_id,
            'payment_method': payment_method,
            'customer_id': str(customer_id)[:255],
        }
        if description:
            payload['description'] = description[:512]
        if redirect_success_url:
            payload['redirect_success_url'] = redirect_success_url[:1024]
        if redirect_fail_url:
            payload['redirect_fail_url'] = redirect_fail_url[:1024]

        logger.info(
            'cisPay API create_payment',
            order_id=order_id,
            amount_kopeks=amount_kopeks,
            payment_method=payment_method,
        )

        data = await self._request('POST', '/payments', json_payload=payload)

        # По спеке обязателен только id; payment_url — nullable. Если ссылки нет,
        # платёж всё равно создан на стороне cisPay: запись сохраняем, иначе
        # пришедший позже вебхук не найдёт платёж и деньги придётся сверять руками.
        if not data.get('id'):
            logger.error('cisPay create_payment: в ответе нет id транзакции', response_data=data)
            raise CisPayAPIError(200, f'Incomplete create payment response: {data}')

        if not data.get('payment_url'):
            logger.warning(
                'cisPay create_payment: ответ без payment_url',
                order_id=order_id,
                payment_id=data.get('id'),
            )

        logger.info(
            'cisPay API payment created',
            order_id=order_id,
            payment_id=data.get('id'),
            status=data.get('status'),
        )
        return data

    async def check_payment(
        self,
        *,
        payment_id: str | None = None,
        order_id: str | None = None,
    ) -> dict[str, Any]:
        """Получает статус платежа.

        GET /payments/status?id=...|order_id=... — статусы PENDING / PAID /
        FAILED / EXPIRED / REFUNDED.
        """
        params: dict[str, Any] = {}
        if payment_id:
            params['id'] = payment_id
        elif order_id:
            params['order_id'] = order_id
        else:
            raise ValueError('cisPay check_payment: нужен payment_id или order_id')

        logger.info('cisPay check_payment', payment_id=payment_id, order_id=order_id)
        return await self._request('GET', '/payments/status', params=params)

    async def get_store_capabilities(self) -> dict[str, Any]:
        """Возвращает активные методы оплаты магазина и их комиссии.

        GET /store/capabilities
        """
        return await self._request('GET', '/store/capabilities')

    async def get_balance(self) -> dict[str, Any]:
        """Получает баланс мерчанта.

        GET /balance
        """
        return await self._request('GET', '/balance')

    def verify_webhook_signature(self, raw_body: bytes, signature: str | None) -> bool:
        """Верификация подписи вебхука.

        X-Signature — HMAC-SHA256 hex от тела запроса (байты как есть),
        ключ — X-Api-Key магазина.
        """
        try:
            received = (signature or '').strip()
            if not received:
                logger.warning('cisPay webhook: отсутствует X-Signature')
                return False

            if not self.api_key:
                # Без ключа HMAC считался бы от b'' — подпись подделал бы кто угодно
                logger.error('cisPay webhook: не задан API-ключ, проверка подписи невозможна')
                return False

            expected = hmac.new(
                self.api_key.encode('utf-8'),
                msg=raw_body,
                digestmod=hashlib.sha256,
            ).hexdigest()

            if not hmac.compare_digest(expected.lower(), received.lower()):
                logger.warning(
                    'cisPay webhook: invalid signature',
                    expected_prefix=expected[:8],
                    received_prefix=received[:8],
                )
                return False
            return True
        except Exception as error:
            logger.error('cisPay webhook verify error', error=error)
            return False


# Singleton instance
cispay_service = CisPayService()
