"""Коллбек рекуррентной подписки Platega приходит в camelCase, а не PascalCase.

Продовый лог: списание по подписке уходило в обработчик РАЗОВЫХ платежей,
тот не находил локальный платёж (под рекуррентное списание записи в payments
нет вовсе) и отвечал 400 — Platega показывала «Callback delivery failed»,
автопродление не работало, при том что обычная оплата проходила.

    [warning] [app.payments] Platega webhook: платеж не найден
              transaction_id=f11d8822-3b12-4afa-a49b-202be11d5600
    [error] [app.webserver.payments] Platega webhook processing failed

Ключ `transaction_id` там читается как ``payload.get('id')`` — то есть в теле
был строчный `id`, а маршрутизация искала PascalCase `PaymentMethod` /
`SubscriptionId` / `Status`. Весь остальной API Platega тоже camelCase
(исходящее создание подписки шлёт `paymentMethod`/`amount`/`interval`).
"""

from __future__ import annotations

import pytest

from app.services.platega_recurrent import (
    PLATEGA_SUBSCRIPTION_METHOD,
    is_subscription_callback,
    read_callback_fields,
)


CHARGE_CAMEL = {
    'id': 'f11d8822-3b12-4afa-a49b-202be11d5600',
    'status': 'CONFIRMED',
    'paymentMethod': PLATEGA_SUBSCRIPTION_METHOD,
    'subscriptionId': 'ps-1',
    'nextChargeAt': '2026-09-01T00:00:00Z',
}
CHARGE_PASCAL = {
    'Id': 'f11d8822-3b12-4afa-a49b-202be11d5600',
    'Status': 'CONFIRMED',
    'PaymentMethod': PLATEGA_SUBSCRIPTION_METHOD,
    'SubscriptionId': 'ps-1',
    'NextChargeAt': '2026-09-01T00:00:00Z',
}


class TestIsSubscriptionCallback:
    def test_camel_case_charge_is_recognised(self) -> None:
        """Та самая форма из продового лога."""
        assert is_subscription_callback(CHARGE_CAMEL) is True

    def test_pascal_case_charge_is_still_recognised(self) -> None:
        assert is_subscription_callback(CHARGE_PASCAL) is True

    @pytest.mark.parametrize('method', [6, '6'], ids=['int', 'string'])
    def test_payment_method_type_does_not_matter(self, method: object) -> None:
        """JSON-числа Platega могли приехать строкой — `== 6` это не ловило."""
        assert is_subscription_callback({'paymentMethod': method}) is True

    def test_subscription_id_alone_is_enough(self) -> None:
        assert is_subscription_callback({'subscriptionId': 'ps-1', 'status': 'CONFIRMED'}) is True

    @pytest.mark.parametrize(
        'status',
        ['SUBSCRIPTION_CANCELLED', 'subscription_cancelled'],
        ids=['upper', 'lower'],
    )
    def test_subscription_status_prefix_in_any_case(self, status: str) -> None:
        assert is_subscription_callback({'status': status}) is True

    # Обратная сторона: разовый платёж не должен уехать в подписочный
    # обработчик, иначе перестанут зачисляться обычные пополнения.
    @pytest.mark.parametrize(
        'payload',
        [
            {'id': 'tx', 'status': 'CONFIRMED'},
            {'Id': 'tx', 'Status': 'CONFIRMED'},
            {'id': 'tx', 'status': 'CANCELED', 'paymentMethod': 2},
            {},
        ],
        ids=['camel', 'pascal', 'other-method', 'empty'],
    )
    def test_one_off_payload_is_not_a_subscription(self, payload: dict) -> None:
        assert is_subscription_callback(payload) is False


class TestReadCallbackFields:
    def test_both_casings_parse_identically(self) -> None:
        assert read_callback_fields(CHARGE_CAMEL) == read_callback_fields(CHARGE_PASCAL)

    def test_camel_case_fields(self) -> None:
        fields = read_callback_fields(CHARGE_CAMEL)

        assert fields.status == 'CONFIRMED'
        assert fields.subscription_id == 'ps-1'
        assert fields.charge_id == 'f11d8822-3b12-4afa-a49b-202be11d5600'
        assert fields.next_charge_at == '2026-09-01T00:00:00Z'

    def test_missing_fields_are_none(self) -> None:
        fields = read_callback_fields({})

        assert (fields.status, fields.subscription_id, fields.charge_id) == (None, None, None)

    def test_blank_values_are_none(self) -> None:
        """Пустой charge id обязан остаться пустым: на нём стоит защита от
        повторного продления, и строка ' ' её бы обошла."""
        fields = read_callback_fields({'Id': '   ', 'SubscriptionId': '', 'Status': None})

        assert (fields.status, fields.subscription_id, fields.charge_id) == (None, None, None)

    def test_numeric_ids_become_strings(self) -> None:
        """`last_charge_external_id` — строковая колонка; число сломало бы сверку."""
        fields = read_callback_fields({'id': 12345, 'subscriptionId': 777})

        assert fields.charge_id == '12345'
        assert fields.subscription_id == '777'

    def test_empty_duplicate_key_does_not_shadow_the_real_one(self) -> None:
        """`Id` и `id` в одном теле: побеждает непустое значение."""
        assert read_callback_fields({'Id': '', 'id': 'charge-1'}).charge_id == 'charge-1'
        assert read_callback_fields({'id': 'charge-1', 'Id': ''}).charge_id == 'charge-1'
