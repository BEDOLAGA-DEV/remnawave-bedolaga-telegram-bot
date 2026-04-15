"""Интеграция с платёжной системой Robokassa."""

from __future__ import annotations

import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from urllib.parse import urlencode

import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class RobokassaService:
    """Формирование URL оплаты и проверка подписи Robokassa."""

    def __init__(self) -> None:
        self.merchant_login = settings.ROBOKASSA_MERCHANT_LOGIN
        self.password_1 = settings.get_robokassa_password_1()
        self.password_2 = settings.get_robokassa_password_2()
        self.base_url = (settings.ROBOKASSA_BASE_URL or 'https://auth.robokassa.ru/Merchant/Index.aspx').strip()
        self.is_test = bool(settings.ROBOKASSA_IS_TEST)
        self.culture = settings.ROBOKASSA_CULTURE or 'ru'
        self.hash_algo = (settings.ROBOKASSA_HASH_ALGO or 'md5').lower()

    @property
    def is_configured(self) -> bool:
        return bool(
            settings.is_robokassa_enabled()
            and self.merchant_login
            and self.password_1
            and self.password_2
        )

    @staticmethod
    def _format_amount(amount_kopeks: int) -> str:
        """Robokassa принимает сумму в рублях с двумя знаками."""
        amount = (Decimal(amount_kopeks) / Decimal(100)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return f'{amount:.2f}'

    def _hash(self, value: str) -> str:
        algo = self.hash_algo
        data = value.encode('utf-8')
        if algo == 'md5':
            return hashlib.md5(data).hexdigest()
        if algo == 'sha1':
            return hashlib.sha1(data).hexdigest()
        if algo == 'sha256':
            return hashlib.sha256(data).hexdigest()
        if algo == 'sha384':
            return hashlib.sha384(data).hexdigest()
        if algo == 'sha512':
            return hashlib.sha512(data).hexdigest()
        return hashlib.md5(data).hexdigest()

    def build_receipt(self, description: str, amount_kopeks: int) -> dict[str, Any] | None:
        if not settings.ROBOKASSA_INCLUDE_RECEIPT:
            return None
        sum_str = self._format_amount(amount_kopeks)
        return {
            'sno': settings.ROBOKASSA_RECEIPT_SNO or 'npd',
            'items': [
                {
                    'name': (description or 'Пополнение баланса')[:128],
                    'quantity': 1,
                    'sum': float(sum_str),
                    'payment_method': settings.ROBOKASSA_RECEIPT_PAYMENT_METHOD or 'full_payment',
                    'payment_object': settings.ROBOKASSA_RECEIPT_PAYMENT_OBJECT or 'service',
                    'tax': settings.ROBOKASSA_RECEIPT_VAT or 'none',
                }
            ],
        }

    @staticmethod
    def _serialize_receipt(receipt: dict[str, Any]) -> str:
        """Robokassa ожидает JSON без пробелов, URL-кодирование выполнит urlencode."""
        return json.dumps(receipt, ensure_ascii=False, separators=(',', ':'))

    def build_signature(
        self,
        *,
        amount_str: str,
        inv_id: int,
        receipt_str: str | None,
        user_params: dict[str, str] | None = None,
    ) -> str:
        """Формирует SignatureValue для запроса оплаты.

        Формат: MerchantLogin:OutSum[:InvId][:Receipt]:Password1[:Shp_*=value ...]
        Пользовательские параметры (Shp_*) участвуют в подписи в алфавитном порядке.
        """
        parts: list[str] = [self.merchant_login or '', amount_str, str(inv_id)]
        if receipt_str is not None:
            parts.append(receipt_str)
        parts.append(self.password_1 or '')

        if user_params:
            shp_sorted = sorted(user_params.items(), key=lambda item: item[0])
            parts.extend(f'{key}={value}' for key, value in shp_sorted)

        return self._hash(':'.join(parts))

    def build_result_signature(
        self,
        *,
        amount_str: str,
        inv_id: int,
        user_params: dict[str, str] | None = None,
    ) -> str:
        """Формирует ожидаемую подпись для ResultURL/SuccessURL.

        Формат: OutSum:InvId:Password2[:Shp_*=value ...]
        """
        parts: list[str] = [amount_str, str(inv_id), self.password_2 or '']
        if user_params:
            shp_sorted = sorted(user_params.items(), key=lambda item: item[0])
            parts.extend(f'{key}={value}' for key, value in shp_sorted)
        return self._hash(':'.join(parts))

    def verify_result_signature(
        self,
        *,
        amount_str: str,
        inv_id: int,
        signature: str,
        user_params: dict[str, str] | None = None,
    ) -> bool:
        if not signature:
            return False
        expected = self.build_result_signature(
            amount_str=amount_str,
            inv_id=inv_id,
            user_params=user_params,
        )
        return expected.lower() == signature.lower()

    def build_payment_url(
        self,
        *,
        amount_kopeks: int,
        inv_id: int,
        description: str,
        user_params: dict[str, str] | None = None,
        inc_curr_label: str | None = None,
    ) -> str | None:
        if not self.is_configured:
            logger.error('Robokassa service is not configured')
            return None

        amount_str = self._format_amount(amount_kopeks)
        receipt = self.build_receipt(description, amount_kopeks)
        receipt_str = self._serialize_receipt(receipt) if receipt else None

        signature = self.build_signature(
            amount_str=amount_str,
            inv_id=inv_id,
            receipt_str=receipt_str,
            user_params=user_params,
        )

        params: dict[str, str] = {
            'MerchantLogin': self.merchant_login or '',
            'OutSum': amount_str,
            'InvId': str(inv_id),
            'Description': (description or 'Пополнение баланса')[:100],
            'SignatureValue': signature,
            'Culture': self.culture,
        }
        if self.hash_algo and self.hash_algo != 'md5':
            params['SignatureAlgorithm'] = self.hash_algo
        if receipt_str is not None:
            params['Receipt'] = receipt_str
        if self.is_test:
            params['IsTest'] = '1'
        if inc_curr_label:
            params['IncCurrLabel'] = inc_curr_label
        if user_params:
            for key, value in user_params.items():
                params[key] = value

        return f'{self.base_url}?{urlencode(params, doseq=False)}'


robokassa_service = RobokassaService()
