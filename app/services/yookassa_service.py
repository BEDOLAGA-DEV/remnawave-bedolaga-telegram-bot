import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

YOOKASSA_API_BASE_URL = 'https://api.yookassa.ru/v3'


# ---------------------------------------------------------------------------
# YooKassa SDK hardening: socket timeout + isolated thread pool.
#
# Root-cause incident: yookassa==3.x's ``ApiClient.execute`` calls
# ``requests.Session.request(...)`` WITHOUT a timeout. When the YooKassa
# API hangs (degradation, network drop), the synchronous call blocks on
# ``socket.recv()`` for hours until TCP keep-alive eventually kills it.
#
# We invoke the SDK via ``loop.run_in_executor(None, ...)`` which uses
# the default process-wide ``ThreadPoolExecutor`` (cap 8-12 threads).
# A wave of slow/dead YK requests fills every slot, the event loop
# starves on every subsequent ``run_in_executor`` (DNS, TCP-connect),
# and the bot effectively freezes — aiogram handlers stop replying
# ('query is too old'), aiohttp to RemnaWave reports
# ``ConnectionTimeoutError`` (event loop starvation, not RemnaWave).
#
# Two defences applied at module import:
#
#   1. **Monkey-patch ``ApiClient.execute``** to pass
#      ``timeout=(connect, read)``. The thread is guaranteed to exit
#      within ``read`` seconds even if the API is dead. ``asyncio.timeout``
#      around the executor call (which we keep for belt-and-suspenders)
#      cancels the coroutine but DOES NOT kill the underlying thread —
#      only the socket timeout does.
#
#   2. **Dedicated ``ThreadPoolExecutor``** with a small bounded size
#      (``max_workers=4``). If all 4 slots fill with stuck YK calls,
#      everything else (RemnaWave, DB sync ops, etc.) keeps running on
#      the default executor. The bot degrades gracefully instead of
#      freezing entirely.
# ---------------------------------------------------------------------------


def _patch_yookassa_timeout() -> None:
    """Add a socket-level timeout to ``yookassa.client.ApiClient.execute``.

    Idempotent — checks a ``_timeout_patched`` flag so a hot-reload of
    the module does not double-wrap. Mirrors the upstream method signature
    exactly so we can drop in our own ``session.request`` call with
    the timeout argument added.

    (connect=5s, read=15s) — operators with consistently slow YK can
    bump these via ``YOOKASSA_HTTP_CONNECT_TIMEOUT`` /
    ``YOOKASSA_HTTP_READ_TIMEOUT`` env vars.
    """
    try:
        from yookassa.client import ApiClient
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning('Could not import yookassa.client.ApiClient for timeout patch', error=str(exc))
        return

    if getattr(ApiClient, '_timeout_patched', False):
        return

    connect_timeout = max(1, int(getattr(settings, 'YOOKASSA_HTTP_CONNECT_TIMEOUT', 5) or 5))
    read_timeout = max(1, int(getattr(settings, 'YOOKASSA_HTTP_READ_TIMEOUT', 15) or 15))

    def execute_with_timeout(self, body, method, path, query_params, request_headers):
        session = self.get_session()
        self.log_request(body, method, path, query_params, request_headers)
        try:
            raw_response = session.request(
                method,
                self.endpoint + path,
                params=query_params,
                headers=request_headers,
                json=body,
                verify=self.configuration.verify,
                timeout=(connect_timeout, read_timeout),
            )
        finally:
            # Match upstream behaviour: close the session even on error.
            # Without this, requests.Session pooled connections leak.
            try:
                session.close()
            except Exception:
                pass
        self.log_response(
            raw_response.content,
            self.get_response_info(raw_response),
            raw_response.headers,
        )
        return raw_response

    ApiClient.execute = execute_with_timeout
    ApiClient._timeout_patched = True
    logger.info(
        'YooKassa ApiClient.execute monkey-patched with HTTP timeout',
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
    )


# Dedicated executor for all synchronous YK SDK calls. Bounded so YK
# slowness can never starve the rest of the bot. ``thread_name_prefix``
# makes stack traces / py-spy output identifiable.
#
# Pool size is operator-tunable via ``settings.YOOKASSA_MAX_CONCURRENT_REQUESTS``
# (default 4). Floored at 1 so a misconfigured ``0`` doesn't disable
# YK entirely; the floor matches the same defensive pattern used for
# the timeout values above.
def _resolve_max_workers() -> int:
    raw = getattr(settings, 'YOOKASSA_MAX_CONCURRENT_REQUESTS', 4)
    try:
        value = int(raw or 4)
    except (TypeError, ValueError):
        value = 4
    return max(1, value)


_yookassa_executor: ThreadPoolExecutor = ThreadPoolExecutor(
    max_workers=_resolve_max_workers(),
    thread_name_prefix='yookassa-sdk',
)


# Apply the patch at import time. Idempotent.
_patch_yookassa_timeout()


class YooKassaService:
    def __init__(
        self,
        shop_id: str | None = None,
        secret_key: str | None = None,
        configured_return_url: str | None = None,
        bot_username_for_default_return: str | None = None,
        scope: str | None = None,
    ):
        yookassa_config = settings.get_yookassa_config(scope)
        shop_id = shop_id or yookassa_config.shop_id
        secret_key = secret_key or yookassa_config.secret_key
        configured_return_url = configured_return_url or yookassa_config.return_url

        self.shop_id = shop_id
        self.secret_key = secret_key
        self.scope = scope
        self.configured = bool(shop_id and secret_key)

        if not shop_id or not secret_key:
            logger.warning(
                'YooKassa SHOP_ID или SECRET_KEY не настроены в settings. Функционал платежей будет ОТКЛЮЧЕН.'
            )
        else:
            logger.info('YooKassa HTTP client configured for shop_id prefix', shop_id_prefix=shop_id[:5], scope=scope)

        if not self.configured:
            self.return_url = 'https://t.me/'
            logger.warning('YooKassa не активна, используем заглушку return_url', return_url=self.return_url)
        elif configured_return_url:
            self.return_url = configured_return_url
        elif bot_username_for_default_return:
            self.return_url = f'https://t.me/{bot_username_for_default_return}'
            logger.info('YOOKASSA_RETURN_URL не установлен, используем бота', return_url=self.return_url)
        else:
            self.return_url = 'https://t.me/'
            logger.warning(
                'КРИТИЧНО: YOOKASSA_RETURN_URL не установлен И username бота не предоставлен. Используем заглушку. Платежи могут работать некорректно.',
                return_url=self.return_url,
            )

        logger.info('YooKassa Service return_url', return_url=self.return_url)

    def _auth(self) -> httpx.BasicAuth:
        return httpx.BasicAuth(self.shop_id or '', self.secret_key or '')

    def _timeout_seconds(self) -> float:
        read_timeout = max(1, int(getattr(settings, 'YOOKASSA_HTTP_READ_TIMEOUT', 15) or 15))
        connect_timeout = max(1, int(getattr(settings, 'YOOKASSA_HTTP_CONNECT_TIMEOUT', 5) or 5))
        return float(max(read_timeout, connect_timeout))

    @staticmethod
    def _amount_value(amount: float) -> str:
        return f'{amount:.2f}'

    @staticmethod
    def _payment_response_to_dict(data: dict[str, Any]) -> dict[str, Any]:
        amount = data.get('amount') or {}
        confirmation = data.get('confirmation') or {}
        payment_method = data.get('payment_method') or {}
        payment_card = payment_method.get('card') if isinstance(payment_method, dict) else None

        result = {
            'id': data.get('id'),
            'confirmation_url': confirmation.get('confirmation_url'),
            'qr_confirmation_data': confirmation.get('confirmation_data'),
            'confirmation': confirmation,
            'status': data.get('status'),
            'metadata': data.get('metadata') or {},
            'amount_value': float(amount.get('value', 0) or 0),
            'amount_currency': amount.get('currency'),
            'paid': bool(data.get('paid', False)),
            'refundable': bool(data.get('refundable', False)),
            'created_at': data.get('created_at'),
            'captured_at': data.get('captured_at'),
            'description_from_yk': data.get('description'),
            'description': data.get('description'),
            'test_mode': data.get('test'),
        }

        if payment_method:
            result.update(
                {
                    'payment_method_type': payment_method.get('type'),
                    'payment_method_id': payment_method.get('id'),
                    'payment_method_saved': bool(payment_method.get('saved', False)),
                    'payment_method_card': payment_card,
                }
            )

        return result

    def _receipt_data(
        self,
        amount: float,
        currency: str,
        description: str,
        receipt_email: str | None,
        receipt_phone: str | None,
    ) -> dict[str, Any] | None:
        customer_contact_for_receipt = {}
        if receipt_email:
            customer_contact_for_receipt['email'] = receipt_email
        elif receipt_phone:
            customer_contact_for_receipt['phone'] = receipt_phone
        elif hasattr(settings, 'YOOKASSA_DEFAULT_RECEIPT_EMAIL') and settings.YOOKASSA_DEFAULT_RECEIPT_EMAIL:
            customer_contact_for_receipt['email'] = settings.YOOKASSA_DEFAULT_RECEIPT_EMAIL
        else:
            logger.error(
                'КРИТИЧНО: Не предоставлен email/телефон для чека YooKassa и YOOKASSA_DEFAULT_RECEIPT_EMAIL не установлен.'
            )
            return None

        return {
            'customer': customer_contact_for_receipt,
            'items': [
                {
                    'description': description[:128],
                    'quantity': '1.00',
                    'amount': {'value': self._amount_value(amount), 'currency': currency.upper()},
                    'vat_code': int(getattr(settings, 'YOOKASSA_VAT_CODE', 1)),
                    'payment_mode': getattr(settings, 'YOOKASSA_PAYMENT_MODE', 'full_payment'),
                    'payment_subject': getattr(settings, 'YOOKASSA_PAYMENT_SUBJECT', 'service'),
                }
            ],
        }

    async def _post_payment(self, payload: dict[str, Any], idempotence_key: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout_seconds()) as client:
            response = await client.post(
                f'{YOOKASSA_API_BASE_URL}/payments',
                json=payload,
                headers={'Idempotence-Key': idempotence_key},
                auth=self._auth(),
            )
            response.raise_for_status()
            return response.json()

    async def create_payment(
        self,
        amount: float,
        currency: str,
        description: str,
        metadata: dict[str, Any],
        receipt_email: str | None = None,
        receipt_phone: str | None = None,
        return_url: str | None = None,
    ) -> dict[str, Any] | None:
        """Создает платеж в YooKassa"""

        if not self.configured:
            logger.error('YooKassa не сконфигурирован. Невозможно создать платеж.')
            return None

        receipt_data_dict = self._receipt_data(amount, currency, description, receipt_email, receipt_phone)
        if receipt_data_dict is None:
            return {
                'error': True,
                'internal_message': 'Отсутствуют контактные данные для чека YooKassa и не настроен email по умолчанию.',
            }

        try:
            idempotence_key = str(uuid.uuid4())
            payload: dict[str, Any] = {
                'amount': {'value': self._amount_value(amount), 'currency': currency.upper()},
                'capture': True,
                'confirmation': {'type': 'redirect', 'return_url': return_url or self.return_url},
                'description': description,
                'metadata': metadata,
                'receipt': receipt_data_dict,
            }
            if settings.is_yookassa_recurrent_enabled(self.scope) and settings.YOOKASSA_RECURRENT_REQUIRED:
                payload['save_payment_method'] = True

            logger.info(
                'Создание платежа YooKassa',
                idempotence_key=idempotence_key,
                amount=amount,
                currency=currency,
                metadata=metadata,
                receipt_data_dict=receipt_data_dict,
            )

            response_data = await self._post_payment(payload, idempotence_key)

            logger.info(
                'Ответ YooKassa HTTP create payment',
                response_id=response_data.get('id'),
                status=response_data.get('status'),
                paid=response_data.get('paid'),
            )

            result = self._payment_response_to_dict(response_data)
            result['idempotence_key_used'] = idempotence_key
            return result
        except Exception as e:
            logger.error('Ошибка создания платежа YooKassa', error=e, exc_info=True)
            return None

    async def create_sbp_payment(
        self,
        amount: float,
        currency: str,
        description: str,
        metadata: dict[str, Any],
        receipt_email: str | None = None,
        receipt_phone: str | None = None,
        return_url: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.configured:
            logger.error('YooKassa не сконфигурирован. Невозможно создать платеж через СБП.')
            return None

        receipt_data_dict = self._receipt_data(amount, currency, description, receipt_email, receipt_phone)
        if receipt_data_dict is None:
            return {
                'error': True,
                'internal_message': 'Отсутствуют контактные данные для чека YooKassa и не настроен email по умолчанию.',
            }

        try:
            idempotence_key = str(uuid.uuid4())
            payload = {
                'amount': {'value': self._amount_value(amount), 'currency': currency.upper()},
                'capture': True,
                'confirmation': {'type': 'redirect', 'return_url': return_url or self.return_url},
                'description': description,
                'metadata': metadata,
                'payment_method_data': {'type': 'sbp'},
                'receipt': receipt_data_dict,
            }

            logger.info(
                'Создание платежа YooKassa СБП с подтверждением redirect',
                idempotence_key=idempotence_key,
                amount=amount,
                currency=currency,
                metadata=metadata,
                receipt_data_dict=receipt_data_dict,
            )

            response_data = await self._post_payment(payload, idempotence_key)

            logger.info(
                'Ответ YooKassa HTTP create payment (СБП, redirect)',
                response_id=response_data.get('id'),
                status=response_data.get('status'),
                paid=response_data.get('paid'),
            )

            result = self._payment_response_to_dict(response_data)
            result['idempotence_key_used'] = idempotence_key
            return result
        except Exception as e:
            logger.error('Ошибка создания платежа YooKassa СБП', error=e, exc_info=True)
            return None

    async def get_payment_info(self, payment_id_in_yookassa: str) -> dict[str, Any] | None:
        if not self.configured:
            logger.error('YooKassa не сконфигурирован. Невозможно получить информацию о платеже.')
            return None

        try:
            logger.info('Получение информации о платеже YooKassa', payment_id_in_yookassa=payment_id_in_yookassa)

            async with httpx.AsyncClient(timeout=self._timeout_seconds()) as client:
                response = await client.get(
                    f'{YOOKASSA_API_BASE_URL}/payments/{payment_id_in_yookassa}',
                    auth=self._auth(),
                )
                if response.status_code == 404:
                    logger.warning(
                        'Платеж не найден в YooKassa (404)',
                        payment_id_in_yookassa=payment_id_in_yookassa,
                    )
                    return None
                response.raise_for_status()
                payment_info_yk = response.json()

            if payment_info_yk:
                logger.info(
                    'Информация о платеже YooKassa',
                    payment_id_in_yookassa=payment_id_in_yookassa,
                    status=payment_info_yk.get('status'),
                    paid=payment_info_yk.get('paid'),
                )
                return self._payment_response_to_dict(payment_info_yk)
            logger.warning('Платеж не найден в YooKassa', payment_id_in_yookassa=payment_id_in_yookassa)
            return None
        except Exception as e:
            logger.error(
                'Ошибка получения информации о платеже YooKassa',
                payment_id_in_yookassa=payment_id_in_yookassa,
                error=e,
                exc_info=True,
            )
            return None

    async def create_autopayment(
        self,
        amount: float,
        currency: str,
        description: str,
        payment_method_id: str,
        metadata: dict[str, Any],
        receipt_email: str | None = None,
        receipt_phone: str | None = None,
        idempotence_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Создаёт рекуррентный автоплатёж через сохранённый payment_method_id (без confirmation)."""

        if not self.configured:
            logger.error('YooKassa не сконфигурирован. Невозможно создать автоплатёж.')
            return None

        customer_contact_for_receipt = {}
        if receipt_email:
            customer_contact_for_receipt['email'] = receipt_email
        elif receipt_phone:
            customer_contact_for_receipt['phone'] = receipt_phone
        elif hasattr(settings, 'YOOKASSA_DEFAULT_RECEIPT_EMAIL') and settings.YOOKASSA_DEFAULT_RECEIPT_EMAIL:
            customer_contact_for_receipt['email'] = settings.YOOKASSA_DEFAULT_RECEIPT_EMAIL
        else:
            logger.error(
                'КРИТИЧНО: Не предоставлен email/телефон для чека автоплатежа и YOOKASSA_DEFAULT_RECEIPT_EMAIL не установлен.'
            )
            return None

        try:
            if not idempotence_key:
                sub_id = metadata.get('subscription_id', uuid.uuid4())
                idempotence_key = f'autopay_{sub_id}_{datetime.now(UTC).strftime("%Y-%m-%d")}'
            receipt_data_dict = {
                'customer': customer_contact_for_receipt,
                'items': [
                    {
                        'description': description[:128],
                        'quantity': '1.00',
                        'amount': {'value': self._amount_value(amount), 'currency': currency.upper()},
                        'vat_code': int(getattr(settings, 'YOOKASSA_VAT_CODE', 1)),
                        'payment_mode': getattr(settings, 'YOOKASSA_PAYMENT_MODE', 'full_payment'),
                        'payment_subject': getattr(settings, 'YOOKASSA_PAYMENT_SUBJECT', 'service'),
                    }
                ],
            }
            payload = {
                'amount': {'value': self._amount_value(amount), 'currency': currency.upper()},
                'capture': True,
                'payment_method_id': payment_method_id,
                'description': description,
                'metadata': metadata,
                'receipt': receipt_data_dict,
            }

            logger.info(
                'Создание автоплатежа YooKassa',
                amount=amount,
                currency=currency,
                payment_method_id=payment_method_id,
                metadata=metadata,
                idempotence_key=idempotence_key,
            )

            response_data = await self._post_payment(payload, idempotence_key)

            logger.info(
                'Ответ YooKassa автоплатёж',
                response_id=response_data.get('id'),
                status=response_data.get('status'),
                paid=response_data.get('paid'),
            )

            result = self._payment_response_to_dict(response_data)
            result['idempotence_key_used'] = idempotence_key
            return result
        except Exception as e:
            logger.error(
                'Ошибка создания автоплатежа YooKassa',
                payment_method_id=payment_method_id,
                error=e,
                exc_info=True,
            )
            return None
