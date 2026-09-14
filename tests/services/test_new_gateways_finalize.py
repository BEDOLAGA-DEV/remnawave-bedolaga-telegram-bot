"""Зачисление баланса у TabPay и ParityPay.

`_finalize_*_payment` — единственная точка, где деньги попадают на баланс
пользователя, и до сих пор она была замокана во всех тестах. Здесь проверяются
инварианты, ценой ошибки в которых будут реальные деньги: баланс растёт ровно
на сумму счёта, транзакция создаётся с правильным способом оплаты и внешним
идентификатором, повторный вызов не зачисляет второй раз, а покупка с лендинга
не попадает на баланс вовсе.

Сценарии одинаковы для обоих шлюзов: разъехавшаяся реализация упадёт здесь.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.database.crud.paritypay as paritypay_crud
import app.database.crud.tabpay as tabpay_crud
import app.database.crud.transaction as transaction_crud
import app.database.crud.user as user_crud
import app.services.payment.common as payment_common
import app.services.payment.paritypay as paritypay_mixin
import app.services.payment.tabpay as tabpay_mixin
from app.database.models import PaymentMethod, TransactionType
from app.services import payment_service as payment_service_module
from app.services.payment_service import PaymentService


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


GATEWAYS = [
    pytest.param(tabpay_mixin, tabpay_crud, 'tabpay', PaymentMethod.TABPAY, id='tabpay'),
    pytest.param(paritypay_mixin, paritypay_crud, 'paritypay', PaymentMethod.PARITYPAY, id='paritypay'),
]


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def flush(self) -> None:
        return None


class FakeUser:
    def __init__(self, balance_kopeks: int = 0) -> None:
        self.id = 77
        self.telegram_id = 123456
        self.balance_kopeks = balance_kopeks
        self.has_made_first_topup = False
        self.referred_by_id = None
        self.user_promo_groups: list[Any] = []
        self.subscription = None
        self.updated_at = None

    def get_primary_promo_group(self) -> None:
        return None


class FakePayment:
    def __init__(self, prefix: str, *, amount_kopeks: int = 125000, metadata: dict | None = None) -> None:
        self.id = 5
        self.user_id = 77
        self.order_id = f'{prefix}123_abcdef12'
        self.amount_kopeks = amount_kopeks
        self.status = 'success'
        self.is_paid = True
        self.is_test = False
        self.paid_at = None
        self.updated_at = None
        self.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        self.metadata_json = metadata if metadata is not None else {}
        self.transaction_id = None


class FakeTransaction:
    def __init__(self, transaction_id: int = 4242) -> None:
        self.id = transaction_id


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    """Подменяет всё окружение зачисления и отдаёт наблюдаемые вызовы."""
    calls: dict[str, Any] = {'transactions': [], 'side_effects': [], 'referral': [], 'linked': []}
    user = FakeUser()

    async def fake_get_user_by_id(_db: Any, _user_id: int) -> FakeUser | None:
        return calls.get('user_override', user)

    async def fake_get_transaction_by_external_id(_db: Any, external_id: str, method: PaymentMethod) -> Any:
        return calls.get('existing_transaction')

    async def fake_create_transaction(_db: Any, **kwargs: Any) -> FakeTransaction:
        calls['transactions'].append(kwargs)
        return FakeTransaction()

    async def fake_lock_user(_db: Any, u: FakeUser) -> FakeUser:
        return u

    async def fake_side_effects(_db: Any, _tx: Any, **kwargs: Any) -> None:
        calls['side_effects'].append(kwargs)

    async def fake_guest(*_args: Any, **kwargs: Any) -> bool | None:
        calls['guest_kwargs'] = kwargs
        return calls.get('guest_result')

    async def fake_referral(_db: Any, user_id: int, amount: int, _bot: Any) -> None:
        calls['referral'].append((user_id, amount))

    async def fake_cart(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(payment_service_module, 'get_user_by_id', fake_get_user_by_id, raising=False)
    monkeypatch.setattr(
        payment_service_module, 'get_transaction_by_external_id', fake_get_transaction_by_external_id, raising=False
    )
    monkeypatch.setattr(payment_service_module, 'create_transaction', fake_create_transaction, raising=False)
    monkeypatch.setattr(user_crud, 'lock_user_for_update', fake_lock_user, raising=False)
    monkeypatch.setattr(transaction_crud, 'emit_transaction_side_effects', fake_side_effects, raising=False)
    monkeypatch.setattr(payment_common, 'try_fulfill_guest_purchase', fake_guest, raising=False)
    monkeypatch.setattr(payment_common, 'send_cart_notification_after_topup', fake_cart, raising=False)

    from app.services import referral_service

    monkeypatch.setattr(referral_service, 'process_referral_topup', fake_referral, raising=False)

    calls['user'] = user
    return calls


def _service() -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    return service


def _link_patch(monkeypatch: pytest.MonkeyPatch, crud: Any, prefix: str, calls: dict) -> None:
    async def fake_link(_db: Any, *, payment: Any, transaction_id: int) -> Any:
        calls['linked'].append(transaction_id)
        payment.transaction_id = transaction_id
        return payment

    monkeypatch.setattr(crud, f'link_{prefix}_payment_to_transaction', fake_link)


async def _finalize(service: PaymentService, prefix: str, db: Any, payment: Any, trigger: str = 'webhook') -> bool:
    return await getattr(service, f'_finalize_{prefix}_payment')(db, payment, trigger=trigger)


# ---------------------------------------------------------------------------


@pytest.mark.parametrize(('mixin', 'crud', 'prefix', 'method'), GATEWAYS)
@pytest.mark.anyio('asyncio')
async def test_credits_exact_amount_and_creates_transaction(
    monkeypatch: pytest.MonkeyPatch, wired: dict, mixin: Any, crud: Any, prefix: str, method: PaymentMethod
) -> None:
    _link_patch(monkeypatch, crud, prefix, wired)
    payment = FakePayment(prefix, amount_kopeks=125000)
    db = FakeSession()

    assert await _finalize(_service(), prefix, db, payment) is True

    user = wired['user']
    assert user.balance_kopeks == 125000, 'баланс должен вырасти ровно на сумму счёта'

    tx = wired['transactions'][0]
    assert tx['amount_kopeks'] == 125000
    assert tx['payment_method'] is method
    assert tx['type'] is TransactionType.DEPOSIT
    assert tx['external_id'] == payment.order_id, 'внешний id — наш order_id, по нему ловится дубль'
    assert tx['is_completed'] is True

    assert wired['linked'] == [4242]
    assert wired['side_effects'][0]['payment_method'] is method
    assert wired['referral'] == [(77, 125000)]
    assert payment.metadata_json['balance_credited'] is True
    assert payment.metadata_json['balance_change']['old_balance'] == 0
    assert payment.metadata_json['balance_change']['new_balance'] == 125000


@pytest.mark.parametrize(('mixin', 'crud', 'prefix', 'method'), GATEWAYS)
@pytest.mark.anyio('asyncio')
async def test_does_not_credit_twice_when_transaction_linked(
    monkeypatch: pytest.MonkeyPatch, wired: dict, mixin: Any, crud: Any, prefix: str, method: PaymentMethod
) -> None:
    """Платёж уже связан с транзакцией — выходим до всякого зачисления."""
    _link_patch(monkeypatch, crud, prefix, wired)
    payment = FakePayment(prefix)
    payment.transaction_id = 111

    assert await _finalize(_service(), prefix, FakeSession(), payment) is True

    assert wired['user'].balance_kopeks == 0
    assert wired['transactions'] == []


@pytest.mark.parametrize(('mixin', 'crud', 'prefix', 'method'), GATEWAYS)
@pytest.mark.anyio('asyncio')
async def test_existing_transaction_with_credited_marker_does_not_double_credit(
    monkeypatch: pytest.MonkeyPatch, wired: dict, mixin: Any, crud: Any, prefix: str, method: PaymentMethod
) -> None:
    """Транзакция уже есть и баланс уже начислен — второй раз не начисляем."""
    _link_patch(monkeypatch, crud, prefix, wired)
    wired['existing_transaction'] = FakeTransaction(999)
    payment = FakePayment(prefix, metadata={'balance_credited': True})

    assert await _finalize(_service(), prefix, FakeSession(), payment) is True

    assert wired['user'].balance_kopeks == 0
    assert wired['transactions'] == [], 'повторная транзакция создаваться не должна'
    assert wired['linked'] == [999]


@pytest.mark.parametrize(('mixin', 'crud', 'prefix', 'method'), GATEWAYS)
@pytest.mark.anyio('asyncio')
async def test_repeated_finalize_credits_only_once(
    monkeypatch: pytest.MonkeyPatch, wired: dict, mixin: Any, crud: Any, prefix: str, method: PaymentMethod
) -> None:
    """Два прохода подряд (вебхук + сверка по API) дают одно зачисление."""
    _link_patch(monkeypatch, crud, prefix, wired)
    payment = FakePayment(prefix, amount_kopeks=50000)

    await _finalize(_service(), prefix, FakeSession(), payment)
    balance_after_first = wired['user'].balance_kopeks

    await _finalize(_service(), prefix, FakeSession(), payment, trigger='api_check')

    assert balance_after_first == 50000
    assert wired['user'].balance_kopeks == 50000, 'второй проход не должен добавлять денег'


@pytest.mark.parametrize(('mixin', 'crud', 'prefix', 'method'), GATEWAYS)
@pytest.mark.anyio('asyncio')
async def test_guest_purchase_short_circuits_without_crediting_balance(
    monkeypatch: pytest.MonkeyPatch, wired: dict, mixin: Any, crud: Any, prefix: str, method: PaymentMethod
) -> None:
    """Покупка с лендинга выдаёт товар, а не пополняет баланс."""
    _link_patch(monkeypatch, crud, prefix, wired)
    wired['guest_result'] = True
    payment = FakePayment(prefix, metadata={'purchase_token': 'tok-1'})

    assert await _finalize(_service(), prefix, FakeSession(), payment) is True

    assert wired['user'].balance_kopeks == 0
    assert wired['transactions'] == []
    assert wired['guest_kwargs']['provider_name'] == prefix
    assert wired['guest_kwargs']['payment_amount_kopeks'] == payment.amount_kopeks
    assert wired['guest_kwargs']['provider_payment_id'] == payment.order_id


@pytest.mark.parametrize(('mixin', 'crud', 'prefix', 'method'), GATEWAYS)
@pytest.mark.anyio('asyncio')
async def test_missing_user_refuses_to_credit(
    monkeypatch: pytest.MonkeyPatch, wired: dict, mixin: Any, crud: Any, prefix: str, method: PaymentMethod
) -> None:
    _link_patch(monkeypatch, crud, prefix, wired)
    wired['user_override'] = None
    payment = FakePayment(prefix)

    assert await _finalize(_service(), prefix, FakeSession(), payment) is False
    assert wired['transactions'] == []


@pytest.mark.parametrize(('mixin', 'crud', 'prefix', 'method'), GATEWAYS)
@pytest.mark.anyio('asyncio')
async def test_referral_failure_does_not_block_crediting(
    monkeypatch: pytest.MonkeyPatch, wired: dict, mixin: Any, crud: Any, prefix: str, method: PaymentMethod
) -> None:
    """Реферальная логика вторична: её падение не должно отменять пополнение."""
    _link_patch(monkeypatch, crud, prefix, wired)

    from app.services import referral_service

    async def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError('referral down')

    monkeypatch.setattr(referral_service, 'process_referral_topup', boom, raising=False)
    payment = FakePayment(prefix, amount_kopeks=30000)

    assert await _finalize(_service(), prefix, FakeSession(), payment) is True
    assert wired['user'].balance_kopeks == 30000


@pytest.mark.anyio('asyncio')
async def test_tabpay_sandbox_payment_never_credits(monkeypatch: pytest.MonkeyPatch, wired: dict) -> None:
    """У TabPay есть песочница: такой платёж не должен доходить до зачисления."""
    _link_patch(monkeypatch, tabpay_crud, 'tabpay', wired)
    payment = FakePayment('tabpay')
    payment.is_test = True

    assert await _finalize(_service(), 'tabpay', FakeSession(), payment) is True

    assert wired['user'].balance_kopeks == 0
    assert wired['transactions'] == []
