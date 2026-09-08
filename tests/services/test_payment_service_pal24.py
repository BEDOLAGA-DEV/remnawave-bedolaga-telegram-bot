"""Тесты Pal24 сценариев PaymentService."""

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings
from app.services.pal24_service import Pal24APIError
from app.services.payment_service import PaymentService


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


class DummySession:
    async def commit(self) -> None:  # pragma: no cover
        return None


class DummyLocalPayment:
    def __init__(self, payment_id: int = 404) -> None:
        self.id = payment_id
        self.created_at = datetime(2024, 1, 2, 10, 0, 0, tzinfo=UTC)


class StubPal24Service:
    def __init__(
        self,
        *,
        configured: bool = True,
        response: dict[str, Any] | None = None,
    ) -> None:
        self.is_configured = configured
        self.response = response or {
            'success': True,
            'bill_id': 'BILL-1',
            'transfer_url': 'https://pal24/sbp',
            'link_url': 'https://pal24/card',
            'status': 'NEW',
        }
        self.calls: list[dict[str, Any]] = []
        self.raise_error: Exception | None = None
        self.status_response: dict[str, Any] | None = {'status': 'NEW'}
        self.payment_status_response: dict[str, Any] | None = None
        self.bill_payments_response: dict[str, Any] | None = None
        self.status_calls: list[str] = []
        self.payment_status_calls: list[str] = []
        self.bill_payments_calls: list[str] = []

    async def create_bill(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.raise_error:
            raise self.raise_error
        return self.response

    async def get_bill_status(self, bill_id: str) -> dict[str, Any] | None:
        self.status_calls.append(bill_id)
        return self.status_response

    async def get_payment_status(self, payment_id: str) -> dict[str, Any] | None:
        self.payment_status_calls.append(payment_id)
        return self.payment_status_response

    async def get_bill_payments(self, bill_id: str) -> dict[str, Any] | None:
        self.bill_payments_calls.append(bill_id)
        return self.bill_payments_response


def _make_service(stub: StubPal24Service | None) -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    service.pal24_service = stub
    service.mulenpay_service = None
    service.yookassa_service = None
    service.cryptobot_service = None
    service.stars_service = None
    service.heleket_service = None
    return service


@pytest.mark.anyio('asyncio')
async def test_create_pal24_payment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubPal24Service()
    service = _make_service(stub)
    db = DummySession()

    captured_args: dict[str, Any] = {}

    async def fake_create_pal24_payment(*args: Any, **kwargs: Any) -> DummyLocalPayment:
        captured_args.update(kwargs)
        if args:
            captured_args['db_arg'] = args[0]
        return DummyLocalPayment(payment_id=321)

    monkeypatch.setattr(
        'app.services.payment_service.create_pal24_payment',
        fake_create_pal24_payment,
        raising=False,
    )
    monkeypatch.setattr(settings, 'PAL24_MIN_AMOUNT_KOPEKS', 1000, raising=False)
    monkeypatch.setattr(settings, 'PAL24_MAX_AMOUNT_KOPEKS', 1_000_000, raising=False)

    result = await service.create_pal24_payment(
        db=db,
        user_id=15,
        amount_kopeks=50000,
        description='Оплата подписки',
        language='ru',
        ttl_seconds=600,
        payer_email='user@example.com',
        payment_method='card',
    )

    assert result is not None
    assert result['local_payment_id'] == 321
    assert result['bill_id'] == 'BILL-1'
    assert result['payment_method'] == 'card'
    assert result['link_url'] == 'https://pal24/sbp'
    assert result['card_url'] == 'https://pal24/card'
    assert stub.calls and stub.calls[0]['payment_method'] == 'BANK_CARD'
    assert stub.calls and stub.calls[0]['amount_kopeks'] == 50000
    assert 'links' in captured_args['metadata']


@pytest.mark.anyio('asyncio')
async def test_create_pal24_payment_default_method(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubPal24Service()
    service = _make_service(stub)
    db = DummySession()

    async def fake_create_pal24_payment(*args: Any, **kwargs: Any) -> DummyLocalPayment:
        return DummyLocalPayment(payment_id=111)

    monkeypatch.setattr(
        'app.services.payment_service.create_pal24_payment',
        fake_create_pal24_payment,
        raising=False,
    )

    monkeypatch.setattr(settings, 'PAL24_MIN_AMOUNT_KOPEKS', 1000, raising=False)
    monkeypatch.setattr(settings, 'PAL24_MAX_AMOUNT_KOPEKS', 1_000_000, raising=False)

    result = await service.create_pal24_payment(
        db=db,
        user_id=42,
        amount_kopeks=10_000,
        description='Пополнение',
        language='ru',
    )

    assert result is not None
    assert stub.calls and stub.calls[0]['payment_method'] == 'SBP'
    assert result['payment_method'] == 'sbp'


@pytest.mark.anyio('asyncio')
async def test_create_pal24_payment_limits_and_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubPal24Service()
    service = _make_service(stub)
    db = DummySession()

    monkeypatch.setattr(settings, 'PAL24_MIN_AMOUNT_KOPEKS', 5000, raising=False)
    monkeypatch.setattr(settings, 'PAL24_MAX_AMOUNT_KOPEKS', 20_000, raising=False)

    result_low = await service.create_pal24_payment(
        db=db,
        user_id=1,
        amount_kopeks=1000,
        description='Пополнение',
        language='ru',
    )
    assert result_low is None

    result_high = await service.create_pal24_payment(
        db=db,
        user_id=1,
        amount_kopeks=50_000,
        description='Пополнение',
        language='ru',
    )
    assert result_high is None

    service_not_configured = _make_service(StubPal24Service(configured=False))
    result_config = await service_not_configured.create_pal24_payment(
        db=db,
        user_id=1,
        amount_kopeks=10_000,
        description='Пополнение',
        language='ru',
    )
    assert result_config is None


@pytest.mark.anyio('asyncio')
async def test_create_pal24_payment_handles_api_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubPal24Service()
    stub.raise_error = Pal24APIError('api failed')
    service = _make_service(stub)
    db = DummySession()

    monkeypatch.setattr(settings, 'PAL24_MIN_AMOUNT_KOPEKS', 1000, raising=False)
    monkeypatch.setattr(settings, 'PAL24_MAX_AMOUNT_KOPEKS', 10_000, raising=False)

    result = await service.create_pal24_payment(
        db=db,
        user_id=5,
        amount_kopeks=2000,
        description='Пополнение',
        language='ru',
    )
    assert result is None


@pytest.mark.anyio('asyncio')
async def test_get_pal24_payment_status_updates_from_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubPal24Service()
    stub.status_response = {'status': 'SUCCESS'}
    stub.payment_status_response = {
        'success': True,
        'id': 'PAY-1',
        'bill_id': 'BILL-1',
        'status': 'SUCCESS',
        'payment_method': 'SBP',
        'account_amount': '700.00',
        'from_card': '676754******1234',
    }
    stub.bill_payments_response = {
        'data': [
            {
                'id': 'PAY-1',
                'bill_id': 'BILL-1',
                'status': 'SUCCESS',
                'from_card': '676754******1234',
                'payment_method': 'SBP',
            }
        ]
    }

    service = _make_service(stub)
    db = DummySession()

    payment = SimpleNamespace(
        id=99,
        bill_id='BILL-1',
        payment_id=None,
        payment_status='NEW',
        payment_method=None,
        balance_amount=None,
        balance_currency=None,
        payer_account=None,
        status='NEW',
        is_paid=False,
        paid_at=None,
        transaction_id=None,
        user_id=1,
    )

    async def fake_get_by_id(db: DummySession, payment_id: int) -> SimpleNamespace:
        assert payment_id == payment.id
        return payment

    async def fake_update_status(
        db: DummySession,
        payment_obj: SimpleNamespace,
        *,
        status: str,
        **kwargs: Any,
    ) -> SimpleNamespace:
        payment_obj.status = status
        payment_obj.last_status = status
        for key, value in kwargs.items():
            setattr(payment_obj, key, value)
        if 'is_paid' in kwargs:
            payment_obj.is_paid = kwargs['is_paid']
        await db.commit()
        return payment_obj

    async def fake_finalize(
        self: PaymentService,
        db: DummySession,
        payment_obj: Any,
        *,
        payment_id: str | None = None,
        trigger: str,
    ) -> bool:
        return False

    monkeypatch.setattr(
        'app.services.payment_service.get_pal24_payment_by_id',
        fake_get_by_id,
        raising=False,
    )
    monkeypatch.setattr(
        'app.services.payment_service.update_pal24_payment_status',
        fake_update_status,
        raising=False,
    )
    monkeypatch.setattr(
        PaymentService,
        '_finalize_pal24_payment',
        fake_finalize,
        raising=False,
    )

    result = await service.get_pal24_payment_status(db, local_payment_id=payment.id)

    assert result is not None
    assert payment.status == 'SUCCESS'
    assert payment.payment_id == 'PAY-1'
    assert payment.payment_status == 'SUCCESS'
    assert payment.payment_method == 'sbp'
    assert payment.is_paid is True
    assert stub.status_calls == ['BILL-1']
    assert stub.payment_status_calls in ([], ['PAY-1'])
    assert result['remote_status'] == 'SUCCESS'
    assert result['remote_data'] and 'bill_status' in result['remote_data']
