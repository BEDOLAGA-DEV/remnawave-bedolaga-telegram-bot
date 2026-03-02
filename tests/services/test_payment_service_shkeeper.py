"""Тесты для SHKeeper payment mixin."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.services.payment.shkeeper as shkeeper_module
import app.services.payment_service as payment_service_module
from app.services.payment_service import PaymentService


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


def _make_service() -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    service.shkeeper_service = None
    return service


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FinalizeDB:
    def __init__(self, payment: Any, user: Any) -> None:
        self.payment = payment
        self.user = user
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.commits = 0
        self.refresh_count = 0

    async def execute(self, stmt: Any) -> _ScalarResult:
        self.statements.append(stmt)
        if len(self.statements) == 1:
            return _ScalarResult(self.payment)
        if len(self.statements) == 2:
            return _ScalarResult(self.user)
        raise AssertionError('Неожиданный SQL запрос в тесте')

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        # Имитируем автогенерацию ID транзакции базой данных.
        for obj in self.added:
            if getattr(obj, 'id', None) is None:
                obj.id = 501

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _obj: Any) -> None:
        self.refresh_count += 1


@pytest.mark.anyio('asyncio')
async def test_process_webhook_paid_status_requires_sufficient_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    db = object()
    payment = SimpleNamespace(
        id=1,
        user_id=7,
        order_id='ord-1',
        external_id='ord-1',
        status='pending',
        is_paid=False,
        metadata_json={},
    )

    async def fake_get_by_external(_db: Any, external_id: str) -> Any:
        assert _db is db
        assert external_id == 'ord-1'
        return payment

    async def fake_update_status(_db: Any, *, payment: Any, status: str, **kwargs: Any) -> Any:
        assert _db is db
        payment.status = status
        payment.metadata_json = kwargs['metadata_json']
        return payment

    monkeypatch.setattr(payment_service_module, 'get_shkeeper_payment_by_external_id', fake_get_by_external, raising=False)
    monkeypatch.setattr(payment_service_module, 'update_shkeeper_payment_status', fake_update_status, raising=False)
    monkeypatch.setattr(service, '_is_sufficient_amount', lambda *_args, **_kwargs: False)
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_shkeeper_payment', finalize_mock)

    result = await service.process_shkeeper_webhook(
        db,
        {
            'external_id': 'ord-1',
            'status': 'PAID',
            'paid': True,
            'amount_crypto': '0.01',
        },
    )

    assert result is False
    finalize_mock.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_process_webhook_overpaid_finalizes_payment(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    db = object()
    payment = SimpleNamespace(
        id=2,
        user_id=8,
        order_id='ord-2',
        external_id='ord-2',
        status='pending',
        is_paid=False,
        metadata_json={},
    )

    async def fake_get_by_external(_db: Any, external_id: str) -> Any:
        assert _db is db
        assert external_id == 'ord-2'
        return payment

    async def fake_update_status(_db: Any, *, payment: Any, status: str, **_kwargs: Any) -> Any:
        payment.status = status
        return payment

    monkeypatch.setattr(payment_service_module, 'get_shkeeper_payment_by_external_id', fake_get_by_external, raising=False)
    monkeypatch.setattr(payment_service_module, 'update_shkeeper_payment_status', fake_update_status, raising=False)
    monkeypatch.setattr(service, '_is_sufficient_amount', lambda *_args, **_kwargs: True)
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_shkeeper_payment', finalize_mock)

    result = await service.process_shkeeper_webhook(
        db,
        {
            'external_id': 'ord-2',
            'status': 'OVERPAID',
            'amount_crypto': '0.03',
        },
    )

    assert result is True
    finalize_mock.assert_awaited_once()


@pytest.mark.anyio('asyncio')
async def test_get_status_does_not_finalize_if_amount_insufficient(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    db = object()
    payment = SimpleNamespace(
        id=3,
        user_id=9,
        order_id='ord-3',
        external_id='ord-3',
        shkeeper_invoice_id='inv-3',
        amount_crypto='1.00',
        display_amount='1.00',
        status='pending',
        is_paid=False,
        metadata_json={},
    )
    service.shkeeper_service = SimpleNamespace(
        get_invoice_status=AsyncMock(return_value={'status': 'paid', 'paid': True, 'amount_crypto': '0.50'})
    )

    async def fake_get_by_id(_db: Any, payment_id: int) -> Any:
        assert _db is db
        assert payment_id == 3
        return payment

    async def fake_update_status(_db: Any, *, payment: Any, status: str, metadata_json: dict[str, Any], **_kwargs: Any) -> Any:
        payment.status = status
        payment.metadata_json = metadata_json
        return payment

    monkeypatch.setattr(payment_service_module, 'get_shkeeper_payment_by_id', fake_get_by_id, raising=False)
    monkeypatch.setattr(payment_service_module, 'update_shkeeper_payment_status', fake_update_status, raising=False)
    monkeypatch.setattr(service, '_is_sufficient_amount', lambda *_args, **_kwargs: False)
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_shkeeper_payment', finalize_mock)

    result = await service.get_shkeeper_payment_status(db, 3)

    assert result is not None
    assert result['payment'].status == 'paid'
    finalize_mock.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_finalize_shkeeper_payment_uses_row_lock_and_short_circuits_processed() -> None:
    service = _make_service()
    locked_payment = SimpleNamespace(id=11, transaction_id=700, order_id='ord-processed')
    db = _FinalizeDB(payment=locked_payment, user=None)

    result = await service._finalize_shkeeper_payment(db, locked_payment, {'id': 'inv-11'})

    assert result is True
    assert len(db.statements) == 1
    # Проверяем, что запрос платежа строится с FOR UPDATE.
    assert getattr(db.statements[0], '_for_update_arg', None) is not None


@pytest.mark.anyio('asyncio')
async def test_finalize_shkeeper_payment_updates_balance_once(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    locked_payment = SimpleNamespace(
        id=12,
        user_id=42,
        order_id='ord-final',
        amount_kopeks=15000,
        transaction_id=None,
        status='pending',
        is_paid=False,
        paid_at=None,
        callback_payload=None,
        updated_at=None,
        created_at=None,
    )
    user = SimpleNamespace(
        id=42,
        balance_kopeks=1000,
        has_made_first_topup=False,
        telegram_id=None,
        updated_at=None,
    )
    db = _FinalizeDB(payment=locked_payment, user=user)

    class _Emitter:
        async def emit(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr('app.services.event_emitter.event_emitter', _Emitter(), raising=False)
    monkeypatch.setattr('app.services.promo_group_assignment.maybe_assign_promo_group_by_total_spent', _noop, raising=False)
    monkeypatch.setattr('app.services.referral_service.process_referral_topup', _noop, raising=False)
    monkeypatch.setattr(shkeeper_module, 'auto_purchase_saved_cart_after_topup', _noop, raising=False)

    result = await service._finalize_shkeeper_payment(db, locked_payment, {'id': 'inv-12'})

    assert result is True
    assert db.commits == 1
    assert locked_payment.transaction_id == 501
    assert locked_payment.is_paid is True
    assert user.balance_kopeks == 16000
    assert user.has_made_first_topup is True
