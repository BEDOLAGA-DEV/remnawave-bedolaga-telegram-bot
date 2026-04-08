"""Тесты для сценариев Platega в PaymentService."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.services.payment_service as payment_service_module
from app.config import settings
from app.services.payment_service import PaymentService


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


class DummySession:
    async def commit(self) -> None:  # pragma: no cover - no custom logic required
        return None

    async def refresh(self, *_: Any) -> None:  # pragma: no cover - no custom logic required
        return None


class DummyLocalPayment:
    def __init__(self, payment_id: int = 101) -> None:
        self.id = payment_id
        self.created_at = datetime.now(UTC)


class StubPlategaService:
    def __init__(
        self,
        *,
        configured: bool = True,
        response: dict[str, Any] | None = None,
        transaction_payload: dict[str, Any] | None = None,
    ) -> None:
        self.is_configured = configured
        self.response = response or {
            'transactionId': 'trx-001',
            'redirect': 'https://platega.example/pay',
            'status': 'PENDING',
            'expiresIn': 900,
        }
        self.transaction_payload = transaction_payload
        self.calls: list[dict[str, Any]] = []
        self.raise_error: Exception | None = None

    async def create_payment(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(kwargs)
        if self.raise_error:
            raise self.raise_error
        return self.response

    async def get_transaction(self, transaction_id: str) -> dict[str, Any] | None:
        self.calls.append({'transaction_lookup': transaction_id})
        return self.transaction_payload


def _make_service(stub: StubPlategaService | None) -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    service.platega_service = stub
    service.yookassa_service = None
    service.cryptobot_service = None
    service.heleket_service = None
    service.mulenpay_service = None
    service.pal24_service = None
    service.stars_service = None
    service.wata_service = None
    return service


@pytest.mark.anyio('asyncio')
async def test_create_platega_payment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubPlategaService()
    service = _make_service(stub)
    db = DummySession()

    captured_args: dict[str, Any] = {}

    async def fake_create_platega_payment(*args: Any, **kwargs: Any) -> DummyLocalPayment:
        if args:
            captured_args['db_arg'] = args[0]
        captured_args.update(kwargs)
        return DummyLocalPayment(payment_id=777)

    monkeypatch.setattr(
        payment_service_module,
        'create_platega_payment',
        fake_create_platega_payment,
        raising=False,
    )
    monkeypatch.setattr(settings, 'PLATEGA_MIN_AMOUNT_KOPEKS', 10_000, raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_MAX_AMOUNT_KOPEKS', 500_000, raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_CURRENCY', 'RUB', raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_RETURN_URL', 'https://return', raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_FAILED_URL', 'https://failed', raising=False)

    result = await service.create_platega_payment(
        db=db,
        user_id=42,
        amount_kopeks=50_000,
        description='Пополнение счёта',
        language='ru',
        payment_method_code=11,
    )

    assert result is not None
    assert result['local_payment_id'] == 777
    assert result['transaction_id'] == 'trx-001'
    assert result['redirect_url'] == 'https://platega.example/pay'
    assert result['status'] == 'PENDING'
    assert 'correlation_id' in result and len(result['correlation_id']) == 32
    assert captured_args['user_id'] == 42
    assert captured_args['amount_kopeks'] == 50_000
    assert captured_args['payment_method_code'] == 11
    assert captured_args['metadata']['selected_method'] == 11
    assert stub.calls and stub.calls[0]['payment_method'] == 11
    assert stub.calls[0]['amount'] == pytest.approx(500.0)
    assert stub.calls[0]['currency'] == 'RUB'
    assert captured_args['metadata']['language'] == 'ru'


@pytest.mark.anyio('asyncio')
async def test_create_platega_payment_respects_limits_and_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubPlategaService()
    service = _make_service(stub)
    db = DummySession()

    monkeypatch.setattr(settings, 'PLATEGA_MIN_AMOUNT_KOPEKS', 20_000, raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_MAX_AMOUNT_KOPEKS', 40_000, raising=False)

    too_low = await service.create_platega_payment(
        db=db,
        user_id=1,
        amount_kopeks=10_000,
        description='Пополнение',
        language='ru',
        payment_method_code=2,
    )
    assert too_low is None

    too_high = await service.create_platega_payment(
        db=db,
        user_id=1,
        amount_kopeks=100_000,
        description='Пополнение',
        language='ru',
        payment_method_code=2,
    )
    assert too_high is None

    not_configured_service = _make_service(StubPlategaService(configured=False))
    result = await not_configured_service.create_platega_payment(
        db=db,
        user_id=1,
        amount_kopeks=30_000,
        description='Пополнение',
        language='ru',
        payment_method_code=2,
    )
    assert result is None


@pytest.mark.anyio('asyncio')
async def test_create_platega_payment_handles_service_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubPlategaService()
    stub.raise_error = RuntimeError('network down')
    service = _make_service(stub)
    db = DummySession()

    async def fake_create_platega_payment(*_: Any, **__: Any) -> DummyLocalPayment:
        pytest.fail('local payment must not be created when Platega call fails')

    monkeypatch.setattr(
        payment_service_module,
        'create_platega_payment',
        fake_create_platega_payment,
        raising=False,
    )
    monkeypatch.setattr(settings, 'PLATEGA_MIN_AMOUNT_KOPEKS', 1_000, raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_MAX_AMOUNT_KOPEKS', 1_000_000, raising=False)

    result = await service.create_platega_payment(
        db=db,
        user_id=5,
        amount_kopeks=25_000,
        description='Пополнение',
        language='ru',
        payment_method_code=13,
    )
    assert result is None
    assert stub.calls and 'payment_method' in stub.calls[0]


def test_get_platega_active_methods_parses_and_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        'PLATEGA_ACTIVE_METHODS',
        ' 2, 11 ;12,13,13,invalid ',
        raising=False,
    )

    methods = settings.get_platega_active_methods()

    assert methods == [2, 11, 12, 13]


def test_get_platega_active_methods_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'PLATEGA_ACTIVE_METHODS', '', raising=False)

    methods = settings.get_platega_active_methods()

    assert methods == [2]


def test_platega_method_display_helpers() -> None:
    assert settings.get_platega_method_display_name(11) == 'Карты (RUB)'
    assert settings.get_platega_method_display_title(11) == '💳 Карты (RUB)'
    assert settings.get_platega_method_display_name(999) == 'Метод 999'
    assert settings.get_platega_method_display_title(999) == 'Platega 999'
