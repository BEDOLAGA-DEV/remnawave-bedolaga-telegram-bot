"""Tests for WATA payment mixin."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.database.crud.wata as wata_crud_module
import app.services.payment_service as payment_service_module
from app.config import settings
from app.services.payment.wata import _extract_transaction_id
from app.services.payment_service import PaymentService
from app.services.wata_service import WataAPIError, WataService


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


class DummySession:
    async def commit(self) -> None:  # pragma: no cover - no logic required
        return None

    async def refresh(self, *_: Any) -> None:  # pragma: no cover - no logic required
        return None

    async def flush(self) -> None:  # pragma: no cover - no logic required
        return None


class DummyLocalPayment:
    def __init__(self, payment_id: int = 42) -> None:
        self.id = payment_id
        self.created_at = datetime.now(UTC)


class StubWataService:
    def __init__(self, response: dict[str, Any] | None) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def create_payment_link(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(kwargs)
        return self.response


class DummyWataPayment:
    def __init__(self) -> None:
        self.id = 1
        self.user_id = 42
        self.payment_link_id = 'link-123'
        self.order_id = 'order-123'
        self.amount_kopeks = 15_000
        self.currency = 'RUB'
        self.description = 'Пополнение'
        self.status = 'Opened'
        self.is_paid = False
        self.metadata_json: dict[str, Any] = {}
        self.transaction_id: int | None = None
        self.callback_payload: dict[str, Any] | None = None
        self.terminal_public_id: str | None = None


def _make_service(stub: StubWataService | None) -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    service.wata_service = stub
    service.mulenpay_service = None
    service.pal24_service = None
    service.yookassa_service = None
    service.stars_service = None
    service.cryptobot_service = None
    service.heleket_service = None
    return service


def test_wata_service_format_datetime_accepts_utc() -> None:
    value = datetime(2024, 5, 20, 12, 30, 0, tzinfo=UTC)
    formatted = WataService._format_datetime(value)
    assert formatted == '2024-05-20T12:30:00Z'


def test_wata_service_parse_datetime_returns_naive_utc() -> None:
    parsed = WataService._parse_datetime('2024-05-20T12:30:00Z')
    assert parsed == datetime(2024, 5, 20, 12, 30, 0, tzinfo=UTC)
    assert parsed.tzinfo is not None

    parsed_with_offset = WataService._parse_datetime('2024-05-20T15:30:00+03:00')
    assert parsed_with_offset == datetime(2024, 5, 20, 12, 30, 0, tzinfo=UTC)
    assert parsed_with_offset.tzinfo is not None


@pytest.mark.anyio('asyncio')
async def test_create_wata_payment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    response = {
        'id': '123e4567-e89b-12d3-a456-426614174000',
        'url': 'https://wata.example/link',
        'status': 'Opened',
        'type': 'OneTime',
        'terminalPublicId': 'terminal-id',
        'successRedirectUrl': 'https://example.com/success',
        'failRedirectUrl': 'https://example.com/fail',
        'expirationDateTime': '2030-01-01T00:00:00Z',
    }
    stub = StubWataService(response)
    service = _make_service(stub)
    db = DummySession()

    captured_args: dict[str, Any] = {}

    async def fake_create_wata_payment(**kwargs: Any) -> DummyLocalPayment:
        captured_args.update(kwargs)
        return DummyLocalPayment(payment_id=777)

    monkeypatch.setattr(payment_service_module, 'create_wata_payment', fake_create_wata_payment, raising=False)
    monkeypatch.setattr(settings, 'WATA_MIN_AMOUNT_KOPEKS', 5000, raising=False)
    monkeypatch.setattr(settings, 'WATA_MAX_AMOUNT_KOPEKS', 500_000, raising=False)

    result = await service.create_wata_payment(
        db=db,
        user_id=101,
        amount_kopeks=15000,
        description='Пополнение',
        language='ru',
    )

    assert result is not None
    assert result['local_payment_id'] == 777
    assert result['payment_link_id'] == response['id']
    assert result['payment_url'] == response['url']
    assert captured_args['user_id'] == 101
    assert captured_args['amount_kopeks'] == 15000
    assert captured_args['payment_link_id'] == response['id']
    assert stub.calls and stub.calls[0]['amount_kopeks'] == 15000


@pytest.mark.anyio('asyncio')
async def test_create_wata_payment_respects_amount_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubWataService({'id': 'link'})
    service = _make_service(stub)
    db = DummySession()

    monkeypatch.setattr(settings, 'WATA_MIN_AMOUNT_KOPEKS', 10_000, raising=False)
    monkeypatch.setattr(settings, 'WATA_MAX_AMOUNT_KOPEKS', 20_000, raising=False)

    too_low = await service.create_wata_payment(
        db=db,
        user_id=1,
        amount_kopeks=5_000,
        description='Пополнение',
    )
    assert too_low is None

    too_high = await service.create_wata_payment(
        db=db,
        user_id=1,
        amount_kopeks=25_000,
        description='Пополнение',
    )
    assert too_high is None
    assert not stub.calls


@pytest.mark.anyio('asyncio')
async def test_create_wata_payment_returns_none_without_service() -> None:
    service = _make_service(None)
    db = DummySession()

    result = await service.create_wata_payment(
        db=db,
        user_id=5,
        amount_kopeks=10_000,
        description='Пополнение',
    )
    assert result is None


@pytest.mark.anyio('asyncio')
async def test_process_wata_webhook_updates_status(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service(None)
    db = DummySession()
    payment = DummyWataPayment()
    update_kwargs: dict[str, Any] = {}
    link_lookup_called = False

    async def fake_get_by_order_id(db_arg: Any, order_id: str) -> DummyWataPayment:
        assert db_arg is db
        assert order_id == payment.order_id
        return payment

    async def fake_get_by_link_id(*_: Any, **__: Any) -> DummyWataPayment | None:
        nonlocal link_lookup_called
        link_lookup_called = True
        return None

    async def fake_update_status(
        db_arg: Any,
        *,
        payment: DummyWataPayment,
        **kwargs: Any,
    ) -> DummyWataPayment:
        assert db_arg is db
        update_kwargs.update(kwargs)
        if 'status' in kwargs:
            payment.status = kwargs['status']
        if 'is_paid' in kwargs:
            payment.is_paid = kwargs['is_paid']
        if 'metadata' in kwargs:
            payment.metadata_json = kwargs['metadata']
        if 'callback_payload' in kwargs:
            payment.callback_payload = kwargs['callback_payload']
        if 'terminal_public_id' in kwargs:
            payment.terminal_public_id = kwargs['terminal_public_id']
        return payment

    monkeypatch.setattr(
        payment_service_module,
        'get_wata_payment_by_order_id',
        fake_get_by_order_id,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        'get_wata_payment_by_link_id',
        fake_get_by_link_id,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        'update_wata_payment_status',
        fake_update_status,
        raising=False,
    )

    async def fake_lock(_db: Any, _payment_id: int) -> DummyWataPayment:
        return payment

    monkeypatch.setattr(
        wata_crud_module,
        'get_wata_payment_by_id_for_update',
        fake_lock,
        raising=False,
    )

    payload = {
        'orderId': payment.order_id,
        'transactionStatus': 'Declined',
        'terminalPublicId': 'terminal-001',
    }

    processed = await service.process_wata_webhook(db, payload)

    assert processed is True
    assert link_lookup_called is False
    assert payment.status == 'Declined'
    assert payment.is_paid is False
    assert payment.metadata_json.get('last_webhook') == payload
    assert payment.callback_payload == payload
    assert payment.terminal_public_id == 'terminal-001'
    assert update_kwargs['status'] == 'Declined'
    assert update_kwargs['is_paid'] is False


@pytest.mark.anyio('asyncio')
async def test_process_wata_webhook_finalizes_paid(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service(None)
    db = DummySession()
    payment = DummyWataPayment()
    finalize_called = False

    async def fake_get_by_order_id(*_: Any, **__: Any) -> DummyWataPayment:
        return payment

    async def fake_update_status(
        db_arg: Any,
        *,
        payment: DummyWataPayment,
        **kwargs: Any,
    ) -> DummyWataPayment:
        if 'metadata' in kwargs:
            payment.metadata_json = kwargs['metadata']
        if 'callback_payload' in kwargs:
            payment.callback_payload = kwargs['callback_payload']
        if 'status' in kwargs:
            payment.status = kwargs['status']
        return payment

    async def fake_finalize(
        db_arg: Any,
        payment_arg: DummyWataPayment,
        payload_arg: dict[str, Any],
    ) -> DummyWataPayment:
        nonlocal finalize_called
        finalize_called = True
        payment_arg.is_paid = True
        return payment_arg

    monkeypatch.setattr(
        payment_service_module,
        'get_wata_payment_by_order_id',
        fake_get_by_order_id,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        'get_wata_payment_by_link_id',
        lambda *args, **kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        'update_wata_payment_status',
        fake_update_status,
        raising=False,
    )
    monkeypatch.setattr(
        service,
        '_finalize_wata_payment',
        fake_finalize,
        raising=False,
    )

    async def fake_lock(_db: Any, _payment_id: int) -> DummyWataPayment:
        return payment

    monkeypatch.setattr(
        wata_crud_module,
        'get_wata_payment_by_id_for_update',
        fake_lock,
        raising=False,
    )

    payload = {
        'orderId': payment.order_id,
        'transactionStatus': 'Paid',
        'transactionId': 'tx-001',
    }

    processed = await service.process_wata_webhook(db, payload)

    assert processed is True
    assert finalize_called is True
    assert payment.is_paid is True
    assert payment.metadata_json.get('last_webhook') == payload


@pytest.mark.anyio('asyncio')
async def test_process_wata_webhook_returns_false_when_payment_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(None)
    db = DummySession()

    async def fake_get_by_order_id(*_: Any, **__: Any) -> None:
        return None

    async def fake_get_by_link_id(*_: Any, **__: Any) -> None:
        return None

    async def fail_update(*_: Any, **__: Any) -> None:
        pytest.fail('update_wata_payment_status should not be called')

    monkeypatch.setattr(
        payment_service_module,
        'get_wata_payment_by_order_id',
        fake_get_by_order_id,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        'get_wata_payment_by_link_id',
        fake_get_by_link_id,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        'update_wata_payment_status',
        fail_update,
        raising=False,
    )

    payload = {
        'orderId': 'missing-order',
        'transactionStatus': 'Paid',
    }

    processed = await service.process_wata_webhook(db, payload)

    assert processed is False


class StubWataStatusService:
    """Stub covering the three WATA calls used by ``get_wata_payment_status``."""

    def __init__(
        self,
        *,
        link: dict[str, Any] | None = None,
        transaction: dict[str, Any] | None = None,
        search_items: list[dict[str, Any]] | None = None,
    ) -> None:
        self.link = link
        self.transaction = transaction
        self.search_items = search_items or []
        self.link_calls: list[str] = []
        self.transaction_calls: list[str] = []
        self.search_calls: list[dict[str, Any]] = []

    async def get_payment_link(self, payment_link_id: str) -> dict[str, Any]:
        self.link_calls.append(payment_link_id)
        if self.link is None:
            raise WataAPIError('link expired')
        return self.link

    async def get_transaction(self, transaction_id: str) -> dict[str, Any]:
        self.transaction_calls.append(transaction_id)
        if self.transaction is None:
            raise WataAPIError('no such transaction')
        return self.transaction

    async def search_transactions(self, **kwargs: Any) -> dict[str, Any]:
        self.search_calls.append(kwargs)
        return {'items': list(self.search_items)}


def _patch_status_lookups(
    monkeypatch: pytest.MonkeyPatch,
    payment: DummyWataPayment,
) -> None:
    async def fake_get_by_id(_db: Any, _payment_id: int) -> DummyWataPayment:
        return payment

    async def fake_update_status(
        _db: Any,
        *,
        payment: DummyWataPayment,
        **kwargs: Any,
    ) -> DummyWataPayment:
        if 'status' in kwargs:
            payment.status = kwargs['status']
        if 'metadata' in kwargs:
            payment.metadata_json = kwargs['metadata']
        return payment

    async def fake_lock(_db: Any, _payment_id: int) -> DummyWataPayment:
        return payment

    monkeypatch.setattr(payment_service_module, 'get_wata_payment_by_id', fake_get_by_id, raising=False)
    monkeypatch.setattr(payment_service_module, 'update_wata_payment_status', fake_update_status, raising=False)
    monkeypatch.setattr(wata_crud_module, 'get_wata_payment_by_id_for_update', fake_lock, raising=False)


@pytest.mark.anyio('asyncio')
async def test_search_transactions_uses_v2_path(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WataService(base_url='https://api.wata.pro/api/h2h', access_token='token')
    recorded: dict[str, Any] = {}

    async def fake_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        recorded['method'] = method
        recorded['path'] = path
        recorded['params'] = kwargs.get('params')
        return {'items': []}

    monkeypatch.setattr(service, '_request', fake_request, raising=False)

    await service.search_transactions(
        order_id='order-123',
        payment_link_id='link-123',
        status='Paid',
        limit=5,
    )

    assert recorded['method'] == 'GET'
    assert recorded['path'].endswith('/v2/transactions')
    assert service._build_url(recorded['path']) == 'https://api.wata.pro/api/h2h/v2/transactions'
    assert recorded['params']['orderId'] == 'order-123'
    assert recorded['params']['paymentLinkId'] == 'link-123'
    assert recorded['params']['statuses'] == 'Paid'


@pytest.mark.anyio('asyncio')
async def test_get_wata_payment_status_credits_when_link_is_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = DummyWataPayment()
    stub = StubWataStatusService(
        link={'id': payment.payment_link_id, 'status': 'Opened'},
        search_items=[
            {
                'id': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
                'status': 'Paid',
                'paymentTime': '2026-08-20T10:15:00Z',
            }
        ],
    )
    service = _make_service(None)
    service.wata_service = stub
    db = DummySession()

    _patch_status_lookups(monkeypatch, payment)

    finalized: list[dict[str, Any]] = []

    async def fake_finalize(_db: Any, payment_arg: DummyWataPayment, payload: dict[str, Any]) -> DummyWataPayment:
        finalized.append(payload)
        payment_arg.is_paid = True
        payment_arg.status = 'Paid'
        return payment_arg

    monkeypatch.setattr(service, '_finalize_wata_payment', fake_finalize, raising=False)

    result = await service.get_wata_payment_status(db, payment.id)

    assert result is not None
    assert stub.search_calls, 'transaction search must run even when the link is still Opened'
    assert finalized, 'payment with a Paid transaction must be finalized'
    assert payment.is_paid is True
    assert result['is_paid'] is True


@pytest.mark.anyio('asyncio')
async def test_get_wata_payment_status_ignores_pending_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = DummyWataPayment()
    stub = StubWataStatusService(
        link={'id': payment.payment_link_id, 'status': 'Opened'},
        search_items=[
            {
                'id': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
                'status': 'Pending',
                'paymentTime': '2026-08-20T10:15:00Z',
            }
        ],
    )
    service = _make_service(None)
    service.wata_service = stub
    db = DummySession()

    _patch_status_lookups(monkeypatch, payment)

    async def fail_finalize(*_: Any, **__: Any) -> None:
        pytest.fail('_finalize_wata_payment should not be called for a Pending transaction')

    monkeypatch.setattr(service, '_finalize_wata_payment', fail_finalize, raising=False)

    result = await service.get_wata_payment_status(db, payment.id)

    assert result is not None
    assert payment.is_paid is False
    assert result['is_paid'] is False


@pytest.mark.anyio('asyncio')
async def test_get_wata_payment_status_rejects_zero_payment_time(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = DummyWataPayment()
    stub = StubWataStatusService(
        link={'id': payment.payment_link_id, 'status': 'Opened'},
        search_items=[
            {
                'id': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
                'status': 'Paid',
                'paymentTime': '0001-01-01T00:00:00Z',
            }
        ],
    )
    service = _make_service(None)
    service.wata_service = stub
    db = DummySession()

    _patch_status_lookups(monkeypatch, payment)

    async def fail_finalize(*_: Any, **__: Any) -> None:
        pytest.fail('_finalize_wata_payment must not be called for a zero paymentTime')

    monkeypatch.setattr(service, '_finalize_wata_payment', fail_finalize, raising=False)

    result = await service.get_wata_payment_status(db, payment.id)

    assert result is not None
    assert payment.is_paid is False
    assert result['is_paid'] is False


def test_extract_transaction_id_ignores_payment_link_id() -> None:
    payment = DummyWataPayment()
    remote_link = {
        'id': '11111111-2222-3333-4444-555555555555',
        'status': 'Opened',
    }

    assert _extract_transaction_id(payment, remote_link) is None


def test_extract_transaction_id_accepts_explicit_transaction_field() -> None:
    payment = DummyWataPayment()
    remote_link = {
        'id': '11111111-2222-3333-4444-555555555555',
        'transactionId': '99999999-8888-7777-6666-555555555555',
    }

    assert _extract_transaction_id(payment, remote_link) == '99999999-8888-7777-6666-555555555555'
