"""Regression tests for numeric unit formatting in the settings UI.

``DEVICES_SELECTION_DISABLED_AMOUNT`` — это количество устройств, но эвристика
``_format_numeric_with_unit`` считала любой ключ с подстрокой ``AMOUNT`` денежным
и показывала его в рублях («3 ₽» вместо «3»). Все реальные денежные ключи содержат
``PRICE`` или оканчиваются на ``_KOPEKS``, поэтому голое ``AMOUNT`` из эвристики
убрано. Тесты пинят обе стороны: счётчик устройств — без валюты, деньги — с ₽.

Regression cover for issue #1658.
"""

from __future__ import annotations

import pytest

from app.services.system_settings_service import bot_configuration_service as svc


def test_devices_selection_disabled_amount_is_not_a_price() -> None:
    assert svc.format_value_human('DEVICES_SELECTION_DISABLED_AMOUNT', 3) == '3'


def test_devices_selection_disabled_amount_zero_stays_plain() -> None:
    assert svc.format_value_human('DEVICES_SELECTION_DISABLED_AMOUNT', 0) == '0'


@pytest.mark.parametrize(
    'key',
    [
        'YOOKASSA_MIN_AMOUNT_KOPEKS',
        'CLOUDPAYMENTS_MAX_AMOUNT_KOPEKS',
        'REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS',
        'BASE_SUBSCRIPTION_PRICE',
        'PRICE_30_DAYS',
        'MIN_BALANCE_FOR_AUTOPAY_KOPEKS',
    ],
)
def test_money_keys_keep_currency_formatting(key: str) -> None:
    assert '₽' in svc.format_value_human(key, 5000)
