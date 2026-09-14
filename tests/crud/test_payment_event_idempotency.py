"""Учёт обработанных уведомлений у TabPay и ParityPay.

На этих двух функциях держится идемпотентность зачисления: пара (id, status)
помечается обработанной, и повторная доставка того же события не пополняет
баланс второй раз. Оба провайдера повторяют доставку (TabPay до 7 раз,
ParityPay до 5), так что цена ошибки — двойное зачисление.

Тесты параметризованы по обоим модулям: разъехавшаяся реализация упадёт здесь,
а не в проде.
"""

from __future__ import annotations

from typing import Any

import pytest

import app.database.crud.paritypay as paritypay_crud
import app.database.crud.tabpay as tabpay_crud


MODULES = [
    pytest.param(tabpay_crud, 'tabpay', id='tabpay'),
    pytest.param(paritypay_crud, 'paritypay', id='paritypay'),
]


class FakePayment:
    def __init__(self, processed_events: Any = None) -> None:
        self.processed_events = processed_events


def _fns(module: Any, prefix: str):
    return (
        getattr(module, f'is_{prefix}_event_processed'),
        getattr(module, f'remember_{prefix}_event'),
    )


@pytest.mark.parametrize(('module', 'prefix'), MODULES)
def test_unknown_event_is_not_processed(module: Any, prefix: str) -> None:
    is_processed, _ = _fns(module, prefix)

    assert is_processed(FakePayment([]), 'id-1:PAID') is False
    assert is_processed(FakePayment(None), 'id-1:PAID') is False


@pytest.mark.parametrize(('module', 'prefix'), MODULES)
def test_remembered_event_is_recognised(module: Any, prefix: str) -> None:
    is_processed, remember = _fns(module, prefix)
    payment = FakePayment([])

    remember(payment, 'id-1:PAID')

    assert is_processed(payment, 'id-1:PAID') is True


@pytest.mark.parametrize(('module', 'prefix'), MODULES)
def test_same_id_other_status_is_separate_event(module: Any, prefix: str) -> None:
    """Возврат по оплаченному счёту — другое событие того же платежа."""
    is_processed, remember = _fns(module, prefix)
    payment = FakePayment([])

    remember(payment, 'id-1:PAID')

    assert is_processed(payment, 'id-1:REFUNDED') is False
    remember(payment, 'id-1:REFUNDED')
    assert payment.processed_events == ['id-1:PAID', 'id-1:REFUNDED']


@pytest.mark.parametrize(('module', 'prefix'), MODULES)
def test_remember_is_idempotent(module: Any, prefix: str) -> None:
    _, remember = _fns(module, prefix)
    payment = FakePayment([])

    remember(payment, 'id-1:PAID')
    remember(payment, 'id-1:PAID')

    assert payment.processed_events == ['id-1:PAID']


@pytest.mark.parametrize(('module', 'prefix'), MODULES)
def test_remember_rebuilds_list_instead_of_mutating(module: Any, prefix: str) -> None:
    """SQLAlchemy замечает изменение JSON-колонки только по присваиванию.

    Мутация списка на месте прошла бы все проверки в памяти, но НЕ попала бы в
    UPDATE — и после перезапуска идемпотентность потерялась бы молча.
    """
    _, remember = _fns(module, prefix)
    original = ['id-0:PAID']
    payment = FakePayment(original)

    remember(payment, 'id-1:PAID')

    assert payment.processed_events == ['id-0:PAID', 'id-1:PAID']
    assert payment.processed_events is not original, 'список должен пересобираться, а не мутироваться'
    assert original == ['id-0:PAID'], 'исходный список не должен меняться'


@pytest.mark.parametrize(('module', 'prefix'), MODULES)
def test_remember_handles_none_column(module: Any, prefix: str) -> None:
    """У записей, созданных до появления колонки, там NULL."""
    _, remember = _fns(module, prefix)
    payment = FakePayment(None)

    remember(payment, 'id-1:PAID')

    assert payment.processed_events == ['id-1:PAID']


@pytest.mark.parametrize(('module', 'prefix'), MODULES)
def test_both_providers_expose_the_same_helpers(module: Any, prefix: str) -> None:
    """Расхождение API между провайдерами ломает миксин на ровном месте."""
    assert callable(getattr(module, f'is_{prefix}_event_processed'))
    assert callable(getattr(module, f'remember_{prefix}_event'))
