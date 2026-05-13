"""Сервис для работы с Etoplatezhi (paymentpage.etoplatezhi.ru)."""

import base64
import hashlib
import hmac
from typing import Any
from urllib.parse import urlencode

import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

PAYMENT_PAGE_BASE_URL = 'https://paymentpage.etoplatezhi.ru/payment'


class EtoplatezhiService:
    """Сервис для построения URL платежей и верификации callback-ов Etoplatezhi."""

    @property
    def project_id(self) -> int:
        return settings.ETOPLATEZHI_PROJECT_ID or 0

    @property
    def secret_key(self) -> str:
        return settings.ETOPLATEZHI_SECRET_KEY or ''

    def _flatten_params(
        self,
        params: dict[str, Any],
        prefix: str = '',
        ignore: set[str] | None = None,
    ) -> list[str]:
        """Рекурсивно «сплющивает» вложенные словари в список 'key:value' строк.

        Keys разделяются двоеточием. ``frame_mode`` и ``signature`` игнорируются.
        Booleans приводятся к '1'/'0'.
        Empty arrays (lists) are excluded entirely per Etoplatezhi spec.
        """
        if ignore is None:
            ignore = {'frame_mode', 'signature'}

        entries: list[str] = []
        for key, value in params.items():
            full_key = f'{prefix}:{key}' if prefix else key
            if full_key in ignore or key in ignore:
                continue

            if isinstance(value, dict):
                entries.extend(self._flatten_params(value, prefix=full_key, ignore=ignore))
            elif isinstance(value, list):
                # Empty arrays are excluded entirely per spec
                if not value:
                    continue
                # Non-empty arrays: flatten each element with index as key
                for idx, item in enumerate(value):
                    item_key = f'{full_key}:{idx}'
                    if isinstance(item, dict):
                        entries.extend(self._flatten_params(item, prefix=item_key, ignore=ignore))
                    elif isinstance(item, bool):
                        entries.append(f'{item_key}:{"1" if item else "0"}')
                    elif item is not None:
                        entries.append(f'{item_key}:{item}')
            elif isinstance(value, bool):
                entries.append(f'{full_key}:{"1" if value else "0"}')
            elif value is not None:
                entries.append(f'{full_key}:{value}')

        return entries

    def _sign(self, params: dict[str, Any]) -> str:
        """HMAC-SHA512 + base64 подпись параметров.

        Algorithm:
        1. Flatten nested dicts with ':' separator.
        2. Each leaf → "key:value".
        3. Sort alphabetically by full key string.
        4. Join with ';'.
        5. HMAC-SHA512 with secret_key.
        6. base64-encode the raw digest.
        """
        entries = self._flatten_params(params)
        entries.sort()
        message = ';'.join(entries)

        digest = hmac.new(
            self.secret_key.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha512,
        ).digest()

        return base64.b64encode(digest).decode('utf-8')

    def build_payment_url(
        self,
        *,
        project_id: int,
        payment_id: str,
        payment_amount: int,
        payment_currency: str = 'RUB',
        customer_id: str,
        description: str | None = None,
        callback_url: str | None = None,
        success_url: str | None = None,
        fail_url: str | None = None,
        force_payment_method: str | None = None,
        customer_email: str | None = None,
        language_code: str | None = None,
    ) -> str:
        """Строит URL для редиректа на платёжную страницу Etoplatezhi.

        Args:
            project_id: ID проекта в Etoplatezhi.
            payment_id: Наш internal order_id.
            payment_amount: Сумма в минорных единицах (копейках).
            payment_currency: ISO 4217 код валюты.
            customer_id: Telegram ID или guest-идентификатор покупателя.
            description: Описание платежа.
            callback_url: URL для callback (POST JSON).
            success_url: URL редиректа при успехе.
            fail_url: URL редиректа при ошибке.
            force_payment_method: 'sbp' или 'card' для принудительного выбора.
            customer_email: Email покупателя.
            language_code: Язык интерфейса ('ru', 'en').

        Returns:
            Полный URL с параметрами и подписью.
        """
        params: dict[str, Any] = {
            'project_id': project_id,
            'payment_id': payment_id,
            'payment_amount': payment_amount,
            'payment_currency': payment_currency,
            'customer_id': customer_id,
        }

        if description:
            params['payment_description'] = description
        if callback_url:
            params['merchant_callback_url'] = callback_url
        if success_url:
            params['redirect_success_url'] = success_url
        if fail_url:
            params['redirect_fail_url'] = fail_url
        if force_payment_method:
            params['force_payment_method'] = force_payment_method
        if customer_email:
            params['customer_email'] = customer_email
        if language_code:
            params['language_code'] = language_code

        params['signature'] = self._sign(params)

        logger.info(
            'Etoplatezhi: building payment URL',
            payment_id=payment_id,
            payment_amount=payment_amount,
            customer_id=customer_id,
        )

        return f'{PAYMENT_PAGE_BASE_URL}?{urlencode(params)}'

    def verify_callback_signature(self, payload: dict[str, Any]) -> bool:
        """Верифицирует подпись в callback-е Etoplatezhi.

        Подпись находится внутри JSON body (поле ``signature``).
        Для проверки: удаляем ``signature`` из всех уровней вложенности,
        вычисляем подпись по оставшимся данным и сравниваем.
        """
        try:
            received_signature = payload.get('signature')
            if not received_signature:
                logger.warning('Etoplatezhi callback: отсутствует signature в payload')
                return False

            # Deep-copy payload and strip all 'signature' keys recursively
            cleaned = self._strip_signature_keys(payload)

            expected = self._sign(cleaned)
            return hmac.compare_digest(expected, str(received_signature))

        except Exception as e:
            logger.error('Etoplatezhi callback verify error', error=e)
            return False

    def _strip_signature_keys(self, data: dict[str, Any]) -> dict[str, Any]:
        """Рекурсивно удаляет ключ ``signature`` из словаря и вложенных словарей."""
        result: dict[str, Any] = {}
        for key, value in data.items():
            if key == 'signature':
                continue
            if isinstance(value, dict):
                result[key] = self._strip_signature_keys(value)
            else:
                result[key] = value
        return result


# Singleton instance
etoplatezhi_service = EtoplatezhiService()
