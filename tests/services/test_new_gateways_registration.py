"""Регистрация TabPay и ParityPay в общих точках бота.

Провайдер можно написать безупречно и не подключить: кнопки не появятся, гостевая
покупка уйдёт в отказ, а фоновая сверка не увидит зависшие платежи. Здесь
проверяется именно подключение, для обоих шлюзов одинаковыми сценариями, чтобы
разъехавшаяся регистрация падала тестом, а не в проде.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings
from app.database.crud.transaction import REAL_PAYMENT_METHODS
from app.database.models import PaymentMethod
from app.services.payment_method_config_service import DEFAULT_METHOD_ORDER, _get_method_defaults
from app.services.payment_service import PaymentService, _split_guest_payment_method
from app.utils.payment_utils import get_available_payment_methods, is_payment_method_available


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


# (ключ метода, префикс настроек, имя метода PaymentService)
GATEWAYS = [
    pytest.param('tabpay', 'TABPAY', 'create_tabpay_payment', id='tabpay'),
    pytest.param('paritypay', 'PARITYPAY', 'create_paritypay_payment', id='paritypay'),
]

CREDENTIALS = {
    'TABPAY': {'TABPAY_API_KEY': 'tp_key', 'TABPAY_WEBHOOK_SECRET': 'whsec'},
    'PARITYPAY': {
        'PARITYPAY_SHOP_ID': 'shop',
        'PARITYPAY_SECRET_KEY': 'secret-1',
        'PARITYPAY_CALLBACK_SECRET': 'secret-2',
    },
}


def _enable(monkeypatch: pytest.MonkeyPatch, prefix: str, *, card: bool = False, sbp: bool = False) -> None:
    for key, value in CREDENTIALS[prefix].items():
        monkeypatch.setattr(settings, key, value, raising=False)
    monkeypatch.setattr(settings, f'{prefix}_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, f'{prefix}_CARD_ENABLED', card, raising=False)
    monkeypatch.setattr(settings, f'{prefix}_SBP_ENABLED', sbp, raising=False)


def _disable(monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    monkeypatch.setattr(settings, f'{prefix}_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, f'{prefix}_CARD_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, f'{prefix}_SBP_ENABLED', False, raising=False)


# ---------------------------------------------------------------------------
# Видимость в списке способов пополнения
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(('method', 'prefix', 'creator'), GATEWAYS)
def test_hidden_when_disabled(monkeypatch: pytest.MonkeyPatch, method: str, prefix: str, creator: str) -> None:
    _disable(monkeypatch, prefix)

    ids = {m['id'] for m in get_available_payment_methods()}

    assert not {i for i in ids if i.startswith(method)}


@pytest.mark.parametrize(('method', 'prefix', 'creator'), GATEWAYS)
def test_generic_button_when_no_sub_methods(
    monkeypatch: pytest.MonkeyPatch, method: str, prefix: str, creator: str
) -> None:
    """Саб-методы не настроены — показываем одну кнопку, способ выберет плательщик."""
    _enable(monkeypatch, prefix)

    found = [m for m in get_available_payment_methods() if m['id'].startswith(method)]

    assert [m['id'] for m in found] == [method]
    assert found[0]['callback'] == f'topup_{method}'
    assert found[0]['name']


@pytest.mark.parametrize(('method', 'prefix', 'creator'), GATEWAYS)
def test_sub_methods_replace_generic_button(
    monkeypatch: pytest.MonkeyPatch, method: str, prefix: str, creator: str
) -> None:
    """Включены карта и СБП — общей кнопки быть не должно, иначе их три."""
    _enable(monkeypatch, prefix, card=True, sbp=True)

    ids = [m['id'] for m in get_available_payment_methods() if m['id'].startswith(method)]

    assert sorted(ids) == sorted([f'{method}_card', f'{method}_sbp'])
    assert method not in ids


@pytest.mark.parametrize(('method', 'prefix', 'creator'), GATEWAYS)
def test_availability_predicates(monkeypatch: pytest.MonkeyPatch, method: str, prefix: str, creator: str) -> None:
    _enable(monkeypatch, prefix, card=True, sbp=False)

    assert is_payment_method_available(method) is True
    assert is_payment_method_available(f'{method}_card') is True
    assert is_payment_method_available(f'{method}_sbp') is False

    _disable(monkeypatch, prefix)
    assert is_payment_method_available(method) is False
    assert is_payment_method_available(f'{method}_card') is False


# ---------------------------------------------------------------------------
# Реестры, от которых зависят статистика и настройки
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(('method', 'prefix', 'creator'), GATEWAYS)
def test_present_in_registries(method: str, prefix: str, creator: str) -> None:
    assert method in {m.value for m in PaymentMethod}
    # Иначе шлюз молча выпадет из выручки, партнёрки и отчётов
    assert method in REAL_PAYMENT_METHODS
    # Иначе строка конфига не заведётся и метод не покажется в кабинете
    assert method in DEFAULT_METHOD_ORDER
    assert method in _get_method_defaults()


@pytest.mark.parametrize(('method', 'prefix', 'creator'), GATEWAYS)
def test_sub_options_declared_for_cabinet(method: str, prefix: str, creator: str) -> None:
    options = _get_method_defaults()[method]['available_sub_options']

    assert {o['id'] for o in options} == {'card', 'sbp'}


@pytest.mark.parametrize(('method', 'prefix', 'creator'), GATEWAYS)
def test_guest_method_split(method: str, prefix: str, creator: str) -> None:
    """Кабинет кодирует выбор суффиксом — база и опция должны разделяться."""
    assert _split_guest_payment_method(method) == (method, None)
    assert _split_guest_payment_method(f'{method}_sbp') == (method, 'sbp')
    assert _split_guest_payment_method(f'{method}_card') == (method, 'card')


# ---------------------------------------------------------------------------
# Гостевая покупка с лендинга
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(('method', 'prefix', 'creator'), GATEWAYS)
@pytest.mark.anyio('asyncio')
async def test_guest_payment_routes_to_gateway(
    monkeypatch: pytest.MonkeyPatch, method: str, prefix: str, creator: str
) -> None:
    _enable(monkeypatch, prefix)

    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    create_mock = AsyncMock(
        return_value={'payment_url': 'https://pay.example/x', 'order_id': f'{method}1_abc', 'local_payment_id': 7}
    )
    monkeypatch.setattr(service, creator, create_mock, raising=False)

    async def noop_patch(*_args: Any, **_kwargs: Any) -> None:
        return None

    result = await service.create_guest_payment(
        db=noop_patch,  # в ветку шлюза сессия не уходит: create_* замокан
        amount_kopeks=125000,
        payment_method=f'{method}_sbp',
        description='Покупка',
        purchase_token='tok-1',
        return_url='https://cab.example/result',
    )

    assert result is not None
    assert result['provider'] == method
    assert result['payment_url'] == 'https://pay.example/x'
    create_mock.assert_awaited_once()
    assert create_mock.await_args.kwargs['payment_method_type'] == 'sbp'
    assert create_mock.await_args.kwargs['user_id'] is None


@pytest.mark.parametrize(('method', 'prefix', 'creator'), GATEWAYS)
@pytest.mark.anyio('asyncio')
async def test_guest_payment_refused_when_gateway_disabled(
    monkeypatch: pytest.MonkeyPatch, method: str, prefix: str, creator: str
) -> None:
    _disable(monkeypatch, prefix)

    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    create_mock = AsyncMock()
    monkeypatch.setattr(service, creator, create_mock, raising=False)

    result = await service.create_guest_payment(
        db=None,
        amount_kopeks=125000,
        payment_method=method,
        description='Покупка',
        purchase_token='tok-1',
        return_url='https://cab.example/result',
    )

    assert result is None
    create_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Фоновая сверка: какие статусы считаются незавершёнными
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(('method', 'prefix', 'creator'), GATEWAYS)
def test_pending_predicate(method: str, prefix: str, creator: str) -> None:
    from app.services import payment_verification_service as pvs

    is_pending = getattr(pvs, f'_is_{method}_pending')

    class _P:
        is_paid = False
        status = 'pending'

    assert is_pending(_P()) is True

    _P.status = 'success'
    assert is_pending(_P()) is False
    _P.status = 'expired'
    assert is_pending(_P()) is False
    _P.status = 'pending'
    _P.is_paid = True
    assert is_pending(_P()) is False


@pytest.mark.parametrize(('method', 'prefix', 'creator'), GATEWAYS)
def test_enabled_in_verification_when_configured(
    monkeypatch: pytest.MonkeyPatch, method: str, prefix: str, creator: str
) -> None:
    from app.services import payment_verification_service as pvs

    member = PaymentMethod(method)
    assert member in pvs.SUPPORTED_MANUAL_CHECK_METHODS
    assert member in pvs.SUPPORTED_AUTO_CHECK_METHODS

    _enable(monkeypatch, prefix)
    assert member in pvs.get_enabled_auto_methods()

    _disable(monkeypatch, prefix)
    assert member not in pvs.get_enabled_auto_methods()
