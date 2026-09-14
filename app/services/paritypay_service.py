"""Клиент ParityPay v2 (api.paritypay.net): счета и проверка подписи уведомлений.

Провайдер принимает СБП и карты. Мерчант создаёт счёт, уводит плательщика на
ссылку платёжной формы, результат получает HTTP-уведомлением с подписью.

Суммы у провайдера — РУБЛИ дробным числом, у нас канон — целые копейки,
поэтому все преобразования идут через Decimal: float здесь дал бы копеечные
расхождения на сверке суммы.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal, InvalidOperation
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

_KOPEKS_IN_RUBLE = Decimal(100)


class ParityPayAPIError(Exception):
    """API ParityPay ответил ошибкой (400/404/422)."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f'ParityPay API error ({status_code}): {message}')


class ParityPayNetworkError(Exception):
    """Ответ не получен: обрыв соединения или таймаут.

    Исход запроса НЕИЗВЕСТЕН — счёт мог создаться. Повторять вслепую нельзя:
    сначала сверка по своему order_id.
    """


def amount_to_kopeks(value: Any) -> int | None:
    """Сумма провайдера (рубли) -> целые копейки.

    Принимает и строку ("1250.00" в уведомлении), и число (1500.5 в ответе API).
    Возвращает None, если значение не разбирается или не сводится к целому числу
    копеек: молча округлять деньги нельзя, лучше отказаться от сверки.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        rubles = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None

    kopeks = rubles * _KOPEKS_IN_RUBLE
    if kopeks != kopeks.to_integral_value():
        # Доли копейки означают либо чужой формат, либо порчу данных
        return None
    return int(kopeks)


def kopeks_to_amount(amount_kopeks: int) -> float:
    """Копейки -> рубли для тела запроса.

    JSON не умеет Decimal, поэтому отдаём float, но получаем его из Decimal —
    так значение остаётся ровно двузначным, без хвостов вида 199.00000000000003.
    """
    return float(Decimal(amount_kopeks) / _KOPEKS_IN_RUBLE)


class ParityPayService:
    """Клиент REST API ParityPay v2.

    Аутентификация — заголовки ``X-ShopId`` (UUID кассы) и ``X-SecretKey``
    (секретный ключ №1). Уведомления подписываются ключом №2.
    """

    # Лимит провайдера: 60 запросов в минуту с одного IP — на порядок жёстче
    # обычного, поэтому статусы узнаём уведомлениями, а не опросом.
    RATE_LIMIT_PER_MINUTE = 60

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    @property
    def base_url(self) -> str:
        return (settings.PARITYPAY_BASE_URL or 'https://api.paritypay.net').rstrip('/')

    @property
    def shop_id(self) -> str:
        return settings.PARITYPAY_SHOP_ID or ''

    @property
    def secret_key(self) -> str:
        return settings.PARITYPAY_SECRET_KEY or ''

    @property
    def callback_secret(self) -> str:
        return settings.PARITYPAY_CALLBACK_SECRET or ''

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
            'X-ShopId': self.shop_id,
            'X-SecretKey': self.secret_key,
            'Content-Type': 'application/json',
        }

    @staticmethod
    def _error_message(data: Any) -> str:
        """Формат ошибки провайдера — объект {"error": "текст"}."""
        if isinstance(data, dict) and data.get('error'):
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
                    logger.error('ParityPay API error', url=url, status=response.status, message=message)
                    raise ParityPayAPIError(response.status, message)

                return data if isinstance(data, dict) else {'_raw': data}
        except (aiohttp.ClientError, TimeoutError) as error:
            logger.error('ParityPay API connection error', url=url, error=str(error))
            raise ParityPayNetworkError(str(error)) from error

    async def create_invoice(
        self,
        *,
        order_id: str,
        amount_kopeks: int,
        comment: str | None = None,
        custom_fields: str | None = None,
        service: str | None = None,
        expire_minutes: int | None = None,
        success_url: str | None = None,
        fail_url: str | None = None,
        callback_url: str | None = None,
    ) -> dict[str, Any]:
        """POST /v2/invoice/create — создаёт счёт и возвращает ссылку ``link``.

        Повтор с тем же order_id — 422 «Order id is not unique».
        Блок subscription намеренно не передаётся: подписки требуют отдельного
        согласования с менеджером и здесь не оформляются.
        """
        payload: dict[str, Any] = {
            'order_id': order_id[:64],
            'amount': kopeks_to_amount(amount_kopeks),
        }
        if comment:
            payload['comment'] = comment[:255]
        if custom_fields:
            payload['custom_fields'] = custom_fields
        if service:
            payload['service'] = service
        if expire_minutes:
            payload['expire'] = int(expire_minutes)
        if success_url:
            payload['success_url'] = success_url
        if fail_url:
            payload['fail_url'] = fail_url
        if callback_url:
            payload['callback_url'] = callback_url

        logger.info('ParityPay create_invoice', order_id=order_id, amount_kopeks=amount_kopeks, service=service)

        data = await self._request('POST', '/v2/invoice/create', json_payload=payload)
        if not data or not data.get('id') or not data.get('link'):
            # Без id и ссылки счёт бесполезен: плательщика некуда вести, а
            # пришедшее позже уведомление не с чем сопоставить.
            logger.error('ParityPay create_invoice: неполный ответ', order_id=order_id, response_data=data)
            raise ParityPayAPIError(200, f'Incomplete create invoice response: {data}')

        logger.info(
            'ParityPay invoice created',
            order_id=order_id,
            invoice_id=data.get('id'),
            status=data.get('status'),
        )
        return data

    async def get_invoice(
        self,
        *,
        invoice_id: str | None = None,
        order_id: str | None = None,
    ) -> dict[str, Any] | None:
        """GET /v2/invoice/status — счёт по id процессинга или по нашему order_id."""
        if invoice_id:
            params = {'id': invoice_id}
        elif order_id:
            params = {'order_id': order_id}
        else:
            raise ValueError('ParityPay get_invoice: нужен invoice_id или order_id')
        return await self._request('GET', '/v2/invoice/status', params=params, allow_404=True)

    async def create_invoice_reconciled(self, *, order_id: str, **kwargs: Any) -> dict[str, Any]:
        """Создаёт счёт, безопасно переживая сетевые сбои и дубли order_id.

        Ответ может потеряться уже после того, как счёт записан у провайдера.
        Повторное создание вслепую либо упрётся в 422, либо (с другим order_id)
        заведёт второй счёт на ту же покупку. Поэтому при неизвестном исходе
        сначала сверяемся по своему order_id и переиспользуем найденный счёт.
        """
        try:
            return await self.create_invoice(order_id=order_id, **kwargs)
        except ParityPayAPIError as error:
            if error.status_code == 422:
                # Единственная 422, которую лечит сверка, — занятый order_id.
                existing = await self.get_invoice(order_id=order_id)
                if existing:
                    logger.info('ParityPay: order_id уже занят, используем существующий счёт', order_id=order_id)
                    return existing
                raise
            if error.status_code < 500:
                raise
            logger.warning('ParityPay: 5xx на создании счёта, сверяемся по order_id', order_id=order_id)
        except ParityPayNetworkError:
            logger.warning('ParityPay: ответ на создание не получен, сверяемся по order_id', order_id=order_id)

        return await self._recreate_after_unknown_outcome(order_id=order_id, **kwargs)

    async def _recreate_after_unknown_outcome(self, *, order_id: str, **kwargs: Any) -> dict[str, Any]:
        """Сверка по order_id и, если счёта нет, единственный повтор создания."""
        existing = await self.get_invoice(order_id=order_id)
        if existing:
            logger.info('ParityPay: счёт всё-таки создался, повтор не нужен', order_id=order_id)
            return existing

        try:
            return await self.create_invoice(order_id=order_id, **kwargs)
        except (ParityPayAPIError, ParityPayNetworkError):
            recovered = await self.get_invoice(order_id=order_id)
            if recovered:
                logger.info('ParityPay: счёт найден после неудачного повтора', order_id=order_id)
                return recovered
            raise

    # ------------------------------------------------------------------
    # Подпись HTTP-уведомлений
    # ------------------------------------------------------------------

    @staticmethod
    def parse_callback_body(raw_body: bytes) -> dict[str, Any] | None:
        """Разбирает тело уведомления, СОХРАНЯЯ исходный текст чисел.

        Подпись считается по значениям тела, а Python при обычном разборе
        переписывает числа по-своему: 1200.0 вместо присланного 1200, и подпись
        перестаёт сходиться. ``parse_float``/``parse_int`` возвращают исходную
        подстроку JSON, поэтому и подпись, и бизнес-логика работают ровно с тем,
        что прислал отправитель.
        """
        try:
            data = json.loads(raw_body, parse_float=str, parse_int=str)
        except (ValueError, TypeError) as error:
            logger.error('ParityPay callback: не удалось разобрать JSON', error=str(error))
            return None
        if not isinstance(data, dict):
            logger.error('ParityPay callback: тело не является объектом JSON')
            return None
        return data

    @staticmethod
    def _stringify(value: Any) -> str:
        """Значение поля -> строка для склейки при расчёте подписи."""
        if value is None:
            return ''
        if isinstance(value, bool):
            return 'true' if value else 'false'
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            # В документированных уведомлениях вложенных структур нет. Если
            # появятся — подпись всё равно не сойдётся, поэтому шумим в лог.
            logger.warning('ParityPay callback: вложенная структура в теле, подпись может не сойтись')
            return json.dumps(value, separators=(',', ':'), ensure_ascii=False)
        return str(value)

    @classmethod
    def build_signature_payload(cls, body: dict[str, Any]) -> str:
        """Поля сортируются по ключам, значения склеиваются без разделителей."""
        return ''.join(cls._stringify(body[key]) for key in sorted(body))

    def verify_callback_signature(self, body: dict[str, Any], signature: str | None) -> bool:
        """Проверяет X-SIGNATURE — HMAC-SHA256 на секретном ключе №2."""
        try:
            received = (signature or '').strip()
            if not received:
                logger.warning('ParityPay callback: отсутствует X-SIGNATURE')
                return False

            secret = self.callback_secret
            if not secret:
                # С пустым ключом HMAC считался бы от известного значения —
                # подпись подделал бы кто угодно.
                logger.error('ParityPay callback: не задан ключ подписи, проверка невозможна')
                return False

            payload = self.build_signature_payload(body)
            expected = hmac.new(secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()

            if not hmac.compare_digest(expected, received.lower()):
                logger.warning('ParityPay callback: invalid signature', received_prefix=received[:8])
                return False
            return True
        except Exception as error:
            logger.error('ParityPay callback verify error', error=str(error))
            return False


# Singleton instance
paritypay_service = ParityPayService()
