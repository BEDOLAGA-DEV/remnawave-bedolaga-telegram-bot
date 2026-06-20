"""
Регрессия: REAL_PAYMENT_METHODS должен покрывать ВСЕ платёжные шлюзы.

Раньше список хардкодился вручную, и забытые шлюзы (Jupiter / Donut / Lava) молча
выпадали из всей статистики дохода (сводка, разбивка по методам, партнёрка,
ежедневный отчёт, webapi). Теперь он выводится из enum PaymentMethod (все методы,
кроме MANUAL и BALANCE), поэтому новый шлюз попадает в статистику автоматически.
"""

from app.database.crud.transaction import REAL_PAYMENT_METHODS
from app.database.models import PaymentMethod


def test_includes_previously_missing_gateways():
    """Jupiter / Donut / Lava — те самые забытые шлюзы — теперь учитываются."""
    for method in (PaymentMethod.JUPITER, PaymentMethod.DONUT, PaymentMethod.LAVA):
        assert method.value in REAL_PAYMENT_METHODS


def test_excludes_only_non_gateway_methods():
    """MANUAL (отдельная строка) и BALANCE (двойной счёт) — единственные исключения."""
    assert PaymentMethod.MANUAL.value not in REAL_PAYMENT_METHODS
    assert PaymentMethod.BALANCE.value not in REAL_PAYMENT_METHODS


def test_covers_every_gateway_in_enum():
    """Любой реальный шлюз из enum обязан быть в списке (защита от будущих пропусков)."""
    expected = {m.value for m in PaymentMethod} - {
        PaymentMethod.MANUAL.value,
        PaymentMethod.BALANCE.value,
    }
    assert set(REAL_PAYMENT_METHODS) == expected
    # на сегодня это 24 шлюза
    assert len(REAL_PAYMENT_METHODS) == len(expected)
