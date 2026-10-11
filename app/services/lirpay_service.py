"""Клиент LirPay Integration API v2 (lirpay.org): ссылки оплаты и вебхуки.

Провайдер принимает СБП, крипту и баланс LolzTeam на своей странице
(``https://lirpay.org/pay/{public_id}``). Мерчант создаёт payment link,
уводит плательщика по ссылке, результат приходит подписанным вебхуком
(``payment.succeeded`` и др.).

Аутентификация — пара ключей: публичный ``X-Lirpay-Public-Key`` (lpk_…)
и секретный ``X-Lirpay-Secret-Key`` (lsk_…). Ключи принадлежат окружению
test/live целиком, отдельного флага режима нет. Денежные POST-запросы
требуют заголовок ``Idempotency-Key``.

Суммы в API — строки рублей («129.00») плюс поля ``amount_minor`` и
``decimals``; бот считает копейками, конвертация только через Decimal.
"""

from __future__ import annotations

import hashlib
import hmac
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

_KOPEKS_IN_RUBLE = Decimal(100)


# Способы, которые можно запросить одним методом (method_mode=single).
# По умолчанию используется multi — покупатель выбирает способ на странице.
LIRPAY_METHODS = ('sbp', 'card', 'crypto')


class LirPayAPIError(Exception):
    """LirPay ответил ошибкой (400/401/403/404/409/422/5xx)."""

    def __init__(self, status_code: int, message: str, code: str | None = None) -> None:
        self.status_code = status_code
        self.message = message
        self.code = code
        super().__init__(f'LirPay API error ({status_code}): {message}')


class LirPayNetworkError(Exception):
    """Ответ не получен: обрыв соединения или таймаут.

    Исход запроса НЕИЗВЕСТЕН — ссылка могла создаться (Idempotency-Key
    защищает от дубля), поэтому при повторе ключ сохраняется.
    """


def kopeks_to_amount(amount_kopeks: int) -> str:
    """Копейки → строка рублей с двумя знаками («129.00»).

    LirPay принимает сумму строкой, а бот считает в копейках. Через
    Decimal, чтобы 10_05 копеек не превратились в «100.499999…».
    """
    return str((Decimal(int(amount_kopeks)) / _KOPEKS_IN_RUBLE).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def amount_to_kopeks(value: Any) -> int | None:
    """Строка/число рублей из ответа LirPay → копейки, либо None.

    Принимает и «1250.00», и 1500.5, и amount_minor (целые минорные единицы
    при decimals=2). Молчаливо округлять деньги нельзя: значение, не
    сводящееся к целым копейкам, считается неразобранным.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        rubles = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None

    kopeks = rubles * _KOPEKS_IN_RUBLE
    if kopeks != kopeks.to_integral_value():
        return None
    return int(kopeks)


class LirPayService:
    """Клиент LirPay Integration API v2 (lirpay.org)."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        # Переопределяются тестами; None = читать из настроек.
        self._base_url: str | None = None
        self._public_key: str | None = None
        self._secret_key: str | None = None
        self._webhook_secret: str | None = None

    @property
    def base_url(self) -> str:
        return self._base_url_get()

    @property
    def public_key(self) -> str:
        return self._public_key_get()

    @property
    def secret_key(self) -> str:
        return self._secret_key_get()

    @property
    def webhook_secret(self) -> str:
        return self._webhook_secret_get()

    def _base_url_get(self) -> str:
        return (self._base_url or settings.LIRPAY_BASE_URL or 'https://lirpay.org').rstrip('/')

    def _public_key_get(self) -> str:
        return self._public_key if self._public_key is not None else (settings.LIRPAY_PUBLIC_KEY or '')

    def _secret_key_get(self) -> str:
        return self._secret_key if self._secret_key is not None else (settings.LIRPAY_SECRET_KEY or '')

    def _webhook_secret_get(self) -> str:
        return self._webhook_secret if self._webhook_secret is not None else (settings.LIRPAY_WEBHOOK_SECRET or '')

    def is_test_key(self) -> bool:
        """Наш ключ принадлежит тестовому окружению (lpk_test_…).

        Среда целиком определяется ключом: тестовые ссылки оплачиваются
        эмулятором без денег, поэтому зачисление по ним запрещено — и на
        вебхуке (X-Lirpay-Mode), и на фоновой сверке (этот хелпер: ответ
        GET /payment-links режима не несёт).
        """
        return self.public_key.strip().startswith('lpk_test_')

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {
            'X-Lirpay-Public-Key': self.public_key,
            'X-Lirpay-Secret-Key': self.secret_key,
            'Content-Type': 'application/json',
        }
        if idempotency_key:
            # Денежные POST требуют Idempotency-Key: повтор запроса с тем же
            # ключом и телом вернёт сохранённый ответ, а не создаст второй счёт.
            headers['Idempotency-Key'] = idempotency_key[:255]
        return headers

    @staticmethod
    def _error_fields(data: Any) -> tuple[str, str | None]:
        """Формат ошибок LirPay: {"error": "...", "code": "...", "request_id": "..."}."""
        if isinstance(data, dict):
            return str(data.get('error') or data), data.get('code')
        return str(data), None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        url = f'{self.base_url}/api/v2/integration/{path.lstrip("/")}'
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

                if response.status == 404 and allow_404:
                    return None

                if response.status >= 400:
                    message, code = self._error_fields(data)
                    logger.error(
                        'LirPay API error',
                        url=url,
                        status=response.status,
                        code=code,
                        message=message,
                    )
                    raise LirPayAPIError(response.status, message, code)

                return data if isinstance(data, dict) else {'_raw': data}
        except (aiohttp.ClientError, TimeoutError) as error:
            logger.error('LirPay API connection error', url=url, error=str(error))
            raise LirPayNetworkError(str(error)) from error

    # ------------------------------------------------------------------
    # Проекты
    # ------------------------------------------------------------------

    async def list_projects(self) -> list[dict[str, Any]]:
        """GET /projects — одобренные проекты (нужен UUID для payment link)."""
        data = await self._request('GET', '/projects')
        items = data.get('items') or data.get('projects') or []
        return items if isinstance(items, list) else []

    # ------------------------------------------------------------------
    # Payment links
    # ------------------------------------------------------------------

    async def create_payment_link(
        self,
        *,
        amount_kopeks: int,
        project_id: str,
        display_name: str,
        currency: str = 'RUB',
        customer_id: str | None = None,
        method: str | None = None,
        idempotency_key: str,
        expires_in_minutes: int | None = None,
    ) -> dict[str, Any]:
        """POST /payment-links — создаёт ссылку оплаты.

        Возвращает ``public_id`` и ``payment_link``
        (``https://lirpay.org/pay/{public_id}``). ``method`` — способ,
        зафиксированный через ``method_mode=single``; без него покупатель
        выбирает способ на странице оплаты (``method_mode=multi``).

        ``Idempotency-Key`` обязателен: передаём наш order_id, тогда повтор
        сетевого запроса вернёт ту же ссылку и не создаст второй счёт.
        """
        payload: dict[str, Any] = {
            'display_name': (display_name or 'Пополнение баланса')[:255],
            'amount': kopeks_to_amount(amount_kopeks),
            'currency': currency,
            'project_id': project_id,
            'link_type': 'one_time',
        }
        if method:
            if method not in LIRPAY_METHODS:
                raise ValueError(f'LirPay: неизвестный способ оплаты {method!r}')
            # selected_chain/selected_asset_symbol выбираются на стороне
            # страницы LirPay; здесь фиксируем только семейство способа.
            payload['method_mode'] = 'single'
        else:
            payload['method_mode'] = 'multi'
        if customer_id:
            payload['customer_id'] = str(customer_id)[:255]
        if expires_in_minutes:
            payload['expires_in_minutes'] = int(expires_in_minutes)

        logger.info(
            'LirPay create_payment_link',
            idempotency_key=idempotency_key,
            amount_kopeks=amount_kopeks,
            method=method or 'any',
        )

        data = await self._request('POST', '/payment-links', json_payload=payload, idempotency_key=idempotency_key)

        if not data or not data.get('public_id'):
            logger.error('LirPay create_payment_link: неполный ответ', response_data=data)
            raise LirPayAPIError(200, f'Incomplete create payment link response: {data}')

        if not data.get('payment_link'):
            # TEST-окружение с предвыбранным способом оплачивается сразу и
            # ссылки не отдаёт; в остальных случаях ссылки нет — счёт уже
            # создан, запись сохраняем, иначе вебхук не найдёт платёж.
            logger.warning(
                'LirPay create_payment_link: ответ без ссылки оплаты',
                public_id=data.get('public_id'),
                status=data.get('status'),
            )

        logger.info(
            'LirPay payment link created',
            public_id=data.get('public_id'),
            status=data.get('status'),
            test_mode=bool(data.get('test_mode')),
        )
        return data

    async def get_payment_link(self, public_id: str) -> dict[str, Any] | None:
        """GET /payment-links/{public_id} — статус ссылки.

        Публичные статусы: ``active`` / ``in_progress`` / ``paid`` / ``expired``.
        """
        return await self._request('GET', f'/payment-links/{public_id}', allow_404=True)

    async def cancel_payment_link(self, public_id: str, *, idempotency_key: str) -> dict[str, Any] | None:
        """POST /payment-links/{public_id}/cancel — деактивировать неоплаченную ссылку."""
        return await self._request(
            'POST',
            f'/payment-links/{public_id}/cancel',
            idempotency_key=idempotency_key,
        )

    # ------------------------------------------------------------------
    # Транзакции
    # ------------------------------------------------------------------

    async def list_transactions(
        self,
        *,
        page: int = 1,
        limit: int = 100,
    ) -> dict[str, Any] | None:
        """GET /transactions — история платежей окружения ключа."""
        return await self._request('GET', '/transactions', params={'page': page, 'limit': limit})

    # ------------------------------------------------------------------
    # Подпись вебхуков
    # ------------------------------------------------------------------

    def verify_webhook_signature(self, raw_body: bytes, signature: str | None) -> bool:
        """Верификация подписи вебхука.

        HMAC-SHA256 (hex) от СЫРОГО тела запроса; ключ — секрет вебхука,
        который LirPay отдаёт один раз при настройке вебхука
        (PUT /webhook) и который отличается от секретного ключа API.
        Имя заголовка в доках не зафиксировано — принимаем варианты
        ``X-Lirpay-Signature`` / ``X-Lirpay-Sign`` / ``X-Signature``.
        """
        try:
            received = (signature or '').strip()
            if not received:
                logger.warning('LirPay webhook: отсутствует заголовок подписи')
                return False

            if not self.webhook_secret:
                # Без секрета HMAC считался бы от известного значения —
                # подпись подделал бы кто угодно.
                logger.error('LirPay webhook: не задан секрет вебхуков, проверка невозможна')
                return False

            expected = hmac.new(self.webhook_secret.encode('utf-8'), raw_body, hashlib.sha256).hexdigest()
            # Принимаем и «sha256=<hex>», и просто «<hex>».
            candidate = received.split('=', 1)[1] if received.lower().startswith('sha256=') else received

            if not hmac.compare_digest(candidate.lower(), expected):
                logger.warning('LirPay webhook: подпись не совпала')
                return False
            return True
        except Exception as error:
            logger.exception('LirPay webhook: ошибка проверки подписи', error=error)
            return False


# Singleton instance
lirpay_service = LirPayService()
