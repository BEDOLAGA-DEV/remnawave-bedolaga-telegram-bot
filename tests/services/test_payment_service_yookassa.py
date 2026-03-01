"""Тесты для YooKassa-сценариев PaymentService."""

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
    """Запускаем async-тесты на asyncio, чтобы избежать зависимостей trio."""
    return 'asyncio'


class DummySession:
    """Простейшая заглушка AsyncSession."""

    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True  # type: ignore[attr-defined]


class DummyLocalPayment:
    """Объект, имитирующий локальную запись платежа."""

    def __init__(self, payment_id: int = 101) -> None:
        self.id = payment_id
        self.created_at = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


class StubYooKassaService:
    """Заглушка для SDK, сохраняющая вызовы."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def create_payment(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response

    async def create_sbp_payment(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


def _make_service(yookassa_service: StubYooKassaService | None) -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    service.yookassa_service = yookassa_service
    service.stars_service = None
    service.mulenpay_service = None
    service.pal24_service = None
    service.mulenpay_service = None
    service.cryptobot_service = None
    service.heleket_service = None
    return service


@pytest.mark.anyio('asyncio')
async def test_create_yookassa_payment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Успешное создание платежа формирует корректные метаданные и локальную запись."""

    response = {
        'id': 'yk_123',
        'status': 'pending',
        'confirmation_url': 'https://yookassa.ru/confirm',
        'amount': {'value': '140.00', 'currency': 'RUB'},
        'metadata': {'existing': 'value'},
        'created_at': '2024-01-01T12:00:00Z',
        'test_mode': False,
    }
    service = _make_service(StubYooKassaService(response))
    db = DummySession()

    captured_args: dict[str, Any] = {}

    async def fake_create_yookassa_payment(**kwargs: Any) -> DummyLocalPayment:
        captured_args.update(kwargs)
        return DummyLocalPayment(payment_id=555)

    monkeypatch.setattr(
        payment_service_module,
        'create_yookassa_payment',
        fake_create_yookassa_payment,
        raising=False,
    )
    monkeypatch.setattr(
        type(settings),
        'format_price',
        lambda self, amount: f'{amount / 100:.0f}₽',
        raising=False,
    )

    result = await service.create_yookassa_payment(
        db=db,
        user_id=42,
        amount_kopeks=14000,
        description='Пополнение',
        receipt_email='user@example.com',
        metadata={'custom': 'data'},
    )

    assert result is not None
    assert result['local_payment_id'] == 555
    assert result['yookassa_payment_id'] == 'yk_123'
    assert result['amount_kopeks'] == 14000
    assert result['amount_rubles'] == 140
    assert result['status'] == 'pending'

    assert captured_args['user_id'] == 42
    assert captured_args['metadata_json']['custom'] == 'data'
    assert captured_args['metadata_json']['user_id'] == '42'
    assert captured_args['metadata_json']['amount_kopeks'] == '14000'
    assert isinstance(captured_args['yookassa_created_at'], datetime)


@pytest.mark.anyio('asyncio')
async def test_create_yookassa_payment_returns_none_when_service_missing() -> None:
    """Если сервис не настроен, метод должен вернуть None."""
    service = _make_service(None)
    db = DummySession()
    result = await service.create_yookassa_payment(
        db=db,
        user_id=1,
        amount_kopeks=1000,
        description='Пополнение',
    )
    assert result is None


@pytest.mark.anyio('asyncio')
async def test_create_yookassa_payment_handles_error_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ответ с ключом error должен приводить к None без записи в БД."""
    response = {'error': True}
    service = _make_service(StubYooKassaService(response))
    db = DummySession()

    called = False

    async def fake_create_yookassa_payment(**kwargs: Any) -> DummyLocalPayment:
        nonlocal called
        called = True
        return DummyLocalPayment()

    monkeypatch.setattr(
        payment_service_module,
        'create_yookassa_payment',
        fake_create_yookassa_payment,
        raising=False,
    )

    result = await service.create_yookassa_payment(
        db=db,
        user_id=1,
        amount_kopeks=5000,
        description='Пополнение',
    )
    assert result is None
    assert called is False


@pytest.mark.anyio('asyncio')
async def test_create_yookassa_sbp_payment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Проверяем SBP-сценарий, включая передачу confirmation_token."""

    response = {
        'id': 'yk_sbp_001',
        'status': 'pending',
        'confirmation_url': 'https://yookassa.ru/confirm',
        'confirmation': {'confirmation_token': 'token123'},
        'created_at': '2024-02-01T10:00:00Z',
    }
    service = _make_service(StubYooKassaService(response))
    db = DummySession()

    captured_args: dict[str, Any] = {}

    async def fake_create_yookassa_payment(**kwargs: Any) -> DummyLocalPayment:
        captured_args.update(kwargs)
        return DummyLocalPayment(payment_id=777)

    monkeypatch.setattr(
        payment_service_module,
        'create_yookassa_payment',
        fake_create_yookassa_payment,
        raising=False,
    )

    result = await service.create_yookassa_sbp_payment(
        db=db,
        user_id=7,
        amount_kopeks=25000,
        description='СБП пополнение',
    )

    assert result is not None
    assert result['confirmation_token'] == 'token123'
    assert captured_args['payment_method_type'] == 'sbp'
    assert captured_args['metadata_json']['type'] == 'balance_topup_sbp'


@pytest.mark.anyio('asyncio')
async def test_create_yookassa_sbp_payment_returns_none_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ошибочный ответ СБП не должен создавать запись."""
    response = {'error': 'invalid'}
    service = _make_service(StubYooKassaService(response))
    db = DummySession()

    called = False

    async def fake_create_yookassa_payment(**kwargs: Any) -> DummyLocalPayment:
        nonlocal called
        called = True
        return DummyLocalPayment()

    monkeypatch.setattr(
        payment_service_module,
        'create_yookassa_payment',
        fake_create_yookassa_payment,
        raising=False,
    )

    result = await service.create_yookassa_sbp_payment(
        db=db,
        user_id=1,
        amount_kopeks=1000,
        description='СБП пополнение',
    )
    assert result is None
    assert called is False
