from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.database.crud.overpay as overpay_crud_module
import app.services.payment.overpay as overpay_mixin_module
import app.services.payment_service as payment_service_module
from app.config import settings
from app.services.overpay_service import OverpayAPIError
from app.services.payment.overpay import OVERPAY_STATUS_MAP
from app.services.payment_service import PaymentService


class DummySession:
    async def commit(self) -> None:
        return None

    async def refresh(self, *_: Any) -> None:
        return None

    async def flush(self) -> None:
        return None


class DummyUser:
    def __init__(self) -> None:
        self.telegram_id = 555


class DummyLocalPayment:
    def __init__(self, payment_id: int = 7) -> None:
        self.id = payment_id


class StubOverpayService:
    def __init__(
        self,
        create_response: dict[str, Any] | None = None,
        s2s_response: dict[str, Any] | None = None,
        s2s_error: Exception | None = None,
        redirect_link: str | None = None,
    ) -> None:
        self.create_response = create_response
        self.s2s_response = s2s_response
        self.s2s_error = s2s_error
        self.redirect_link = redirect_link
        self.create_calls: list[dict[str, Any]] = []
        self.s2s_calls: list[dict[str, Any]] = []
        self.wait_calls: list[str] = []

    async def create_payment(self, **kwargs: Any) -> dict[str, Any]:
        self.create_calls.append(kwargs)
        assert self.create_response is not None, 'create_payment must not be called'
        return self.create_response

    async def create_payment_s2s(self, **kwargs: Any) -> dict[str, Any]:
        self.s2s_calls.append(kwargs)
        if self.s2s_error is not None:
            raise self.s2s_error
        assert self.s2s_response is not None, 'create_payment_s2s must not be called'
        return self.s2s_response

    async def wait_for_redirect_link(self, order_id: str) -> str | None:
        self.wait_calls.append(order_id)
        return self.redirect_link


@pytest.fixture
def overpay_settings(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setattr(settings, 'OVERPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_USERNAME', 'login', raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_PASSWORD', 'secret', raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_PROJECT_ID', 'default-project', raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_SBP_TERMINAL_ID', 'sbp-terminal', raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_CARD_TERMINAL_ID', 'card-terminal', raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_INT_TERMINAL_ID', 'int-terminal', raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_INT_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_INT_MIN_EUR', 5.0, raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_RUB_PER_EUR', 100.0, raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_SBP_DIRECT_QR', False, raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_SERVER_IP', '203.0.113.10', raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_MIN_AMOUNT_KOPEKS', 10000, raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_MAX_AMOUNT_KOPEKS', 10000000, raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_RETURN_URL', 'https://t.me/testbot', raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_LIFETIME_MINUTES', 60, raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_CURRENCY', 'RUB', raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_PAYMENT_METHODS', 'card,fps', raising=False)
    return monkeypatch


def _make_service(stub: StubOverpayService, monkeypatch: pytest.MonkeyPatch) -> tuple[PaymentService, dict[str, Any]]:
    service = PaymentService.__new__(PaymentService)
    service.bot = None
    monkeypatch.setattr(overpay_mixin_module, 'overpay_service', stub, raising=False)

    captured: dict[str, Any] = {}

    async def fake_get_user_by_id(db: Any, user_id: int) -> DummyUser:
        return DummyUser()

    async def fake_create_overpay_payment(**kwargs: Any) -> DummyLocalPayment:
        captured.update(kwargs)
        return DummyLocalPayment()

    monkeypatch.setattr(payment_service_module, 'get_user_by_id', fake_get_user_by_id, raising=False)
    monkeypatch.setattr(overpay_crud_module, 'create_overpay_payment', fake_create_overpay_payment, raising=False)
    return service, captured


@pytest.mark.asyncio
async def test_card_option_routes_to_card_terminal(
    overpay_settings: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = StubOverpayService(create_response={'id': 'op-1', 'resultUrl': 'https://pay.overpay.io/form'})
    service, captured = _make_service(stub, monkeypatch)

    result = await service.create_overpay_payment(
        db=DummySession(),
        user_id=101,
        amount_kopeks=50000,
        option='card',
    )

    assert result is not None
    call = stub.create_calls[0]
    assert call['project_id'] == 'card-terminal'
    assert call['payment_methods'] == ['card']
    assert call['currency'] == 'RUB'
    assert result['currency'] == 'RUB'
    assert result['option'] == 'card'
    assert captured['payment_method'] == 'card'
    assert captured['metadata_json']['option'] == 'card'


@pytest.mark.asyncio
async def test_int_option_converts_to_eur(
    overpay_settings: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = StubOverpayService(create_response={'id': 'op-2', 'resultUrl': 'https://pay.overpay.io/form'})
    service, captured = _make_service(stub, monkeypatch)

    result = await service.create_overpay_payment(
        db=DummySession(),
        user_id=101,
        amount_kopeks=100000,
        option='int',
    )

    assert result is not None
    call = stub.create_calls[0]
    assert call['amount'] == '10.00'
    assert call['currency'] == 'EUR'
    assert call['project_id'] == 'int-terminal'
    assert call['payment_methods'] == ['card']
    assert result['amount_eur'] == 10.0
    assert result['amount_kopeks'] == 100000
    assert result['currency'] == 'EUR'
    assert captured['currency'] == 'EUR'
    assert captured['amount_kopeks'] == 100000
    assert captured['metadata_json']['amount_eur'] == 10.0
    assert captured['metadata_json']['rub_per_eur'] == 100.0


@pytest.mark.asyncio
async def test_int_option_below_min_eur_rejected(
    overpay_settings: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = StubOverpayService(create_response={'id': 'op-3', 'resultUrl': 'https://pay'})
    service, _ = _make_service(stub, monkeypatch)

    result = await service.create_overpay_payment(
        db=DummySession(),
        user_id=101,
        amount_kopeks=40000,
        option='int',
    )

    assert result is None
    assert stub.create_calls == []


@pytest.mark.asyncio
async def test_int_option_rejected_when_disabled(
    overpay_settings: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    overpay_settings.setattr(settings, 'OVERPAY_INT_ENABLED', False, raising=False)
    stub = StubOverpayService(create_response={'id': 'op-4', 'resultUrl': 'https://pay'})
    service, _ = _make_service(stub, monkeypatch)

    result = await service.create_overpay_payment(
        db=DummySession(),
        user_id=101,
        amount_kopeks=100000,
        option='int',
    )

    assert result is None
    assert stub.create_calls == []


@pytest.mark.asyncio
async def test_fps_direct_qr_uses_s2s_flow(
    overpay_settings: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    overpay_settings.setattr(settings, 'OVERPAY_SBP_DIRECT_QR', True, raising=False)
    stub = StubOverpayService(
        s2s_response={'id': 'order-9', 'status': 'pending'},
        redirect_link='https://qr.nspk.ru/AS1',
    )
    service, captured = _make_service(stub, monkeypatch)

    result = await service.create_overpay_payment(
        db=DummySession(),
        user_id=101,
        amount_kopeks=50000,
        option='fps',
    )

    assert result is not None
    assert result['payment_url'] == 'https://qr.nspk.ru/AS1'
    assert result['overpay_payment_id'] == 'order-9'
    assert stub.s2s_calls[0]['project_id'] == 'sbp-terminal'
    assert stub.wait_calls == ['order-9']
    assert stub.create_calls == []
    assert captured['metadata_json']['direct_qr'] is True


@pytest.mark.asyncio
async def test_fps_direct_qr_without_link_returns_none(
    overpay_settings: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    overpay_settings.setattr(settings, 'OVERPAY_SBP_DIRECT_QR', True, raising=False)
    stub = StubOverpayService(s2s_response={'id': 'order-10', 'status': 'pending'}, redirect_link=None)
    service, _ = _make_service(stub, monkeypatch)

    result = await service.create_overpay_payment(
        db=DummySession(),
        user_id=101,
        amount_kopeks=50000,
        option='fps',
    )

    assert result is None
    assert stub.create_calls == []


@pytest.mark.asyncio
async def test_fps_direct_qr_s2s_error_falls_back_to_form(
    overpay_settings: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    overpay_settings.setattr(settings, 'OVERPAY_SBP_DIRECT_QR', True, raising=False)
    stub = StubOverpayService(
        create_response={'id': 'op-11', 'resultUrl': 'https://pay.overpay.io/form'},
        s2s_error=OverpayAPIError(500, 'boom'),
    )
    service, captured = _make_service(stub, monkeypatch)

    result = await service.create_overpay_payment(
        db=DummySession(),
        user_id=101,
        amount_kopeks=50000,
        option='fps',
    )

    assert result is not None
    assert result['payment_url'] == 'https://pay.overpay.io/form'
    assert len(stub.create_calls) == 1
    assert 'direct_qr' not in captured['metadata_json']


@pytest.mark.asyncio
async def test_fps_without_direct_qr_uses_form(
    overpay_settings: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = StubOverpayService(create_response={'id': 'op-5', 'resultUrl': 'https://pay.overpay.io/form'})
    service, _ = _make_service(stub, monkeypatch)

    result = await service.create_overpay_payment(
        db=DummySession(),
        user_id=101,
        amount_kopeks=50000,
        option='fps',
    )

    assert result is not None
    call = stub.create_calls[0]
    assert call['payment_methods'] == ['fps']
    assert call['project_id'] == 'sbp-terminal'
    assert stub.s2s_calls == []


@pytest.mark.asyncio
async def test_legacy_call_without_option_keeps_old_behavior(
    overpay_settings: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = StubOverpayService(create_response={'id': 'op-6', 'resultUrl': 'https://pay.overpay.io/form'})
    service, captured = _make_service(stub, monkeypatch)

    result = await service.create_overpay_payment(
        db=DummySession(),
        user_id=101,
        amount_kopeks=50000,
    )

    assert result is not None
    call = stub.create_calls[0]
    assert call['project_id'] == 'default-project'
    assert call['payment_methods'] == ['card', 'fps']
    assert captured['payment_method'] is None


@pytest.mark.asyncio
async def test_guest_payment_forwards_int_option(
    overpay_settings: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = PaymentService.__new__(PaymentService)
    service.bot = None
    received: dict[str, Any] = {}

    async def fake_create_overpay_payment(**kwargs: Any) -> dict[str, Any]:
        received.update(kwargs)
        return {
            'local_payment_id': 7,
            'payment_url': 'https://pay.overpay.io/form',
            'overpay_payment_id': 'op-7',
        }

    async def fake_getter(db: Any, payment_id: int) -> None:
        return None

    monkeypatch.setattr(service, 'create_overpay_payment', fake_create_overpay_payment, raising=False)
    monkeypatch.setattr(overpay_crud_module, 'get_overpay_payment_by_id', fake_getter, raising=False)

    result = await service.create_guest_payment(
        DummySession(),
        amount_kopeks=100000,
        payment_method='overpay_int',
        description='guest',
        purchase_token='tok-1',
        return_url='https://return',
    )

    assert result is not None
    assert result['provider'] == 'overpay'
    assert received['option'] == 'int'


def test_status_map_extended() -> None:
    assert OVERPAY_STATUS_MAP['charged'] == ('success', True)
    assert OVERPAY_STATUS_MAP['authorized'] == ('authorized', False)
    for status in (
        'preflight',
        'new',
        'prepared',
        'prepared_for_holder_metadata_collecting',
        'processing',
        'declined',
        'rejected',
        'error',
        'reversed',
        'refunded',
        'chargeback',
        'representment',
        'credited',
    ):
        _, is_paid = OVERPAY_STATUS_MAP[status]
        assert is_paid is False
    for status in (
        'approved',
        'settled',
        'completed',
        'success',
        'successful',
        'expired',
        'cancelled',
        'failed',
    ):
        assert status not in OVERPAY_STATUS_MAP
