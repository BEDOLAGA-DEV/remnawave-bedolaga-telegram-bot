"""Тесты для сценариев cisPay в PaymentService.

Покрывают создание платежа (маппинг sub-методов на CARD/SBP, лимиты сумм),
обработку вебхука (зачисление, несовпадение суммы, идемпотентность, стики
терминальных статусов) и проверку HMAC-подписи вебхука.
"""

import hashlib
import hmac
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.database.crud.cispay as cispay_crud_module
import app.services.payment.cispay as cispay_mixin_module
import app.services.payment_service as payment_service_module
from app.config import settings
from app.services.cispay_service import CisPayService
from app.services.payment_service import PaymentService


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


class DummySession:
    async def commit(self) -> None:
        return None

    async def refresh(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def flush(self) -> None:
        return None


class DummyLocalPayment:
    def __init__(self, payment_id: int = 501) -> None:
        self.id = payment_id
        self.created_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class FakeCisPayPayment:
    def __init__(
        self,
        *,
        status: str = 'pending',
        is_paid: bool = False,
        amount_kopeks: int = 50000,
    ) -> None:
        self.id = 7
        self.user_id = 77
        self.order_id = 'cis123_abc123'
        self.cispay_payment_id = None
        self.amount_kopeks = amount_kopeks
        self.charged_amount_kopeks = None
        self.status = status
        self.is_paid = is_paid
        self.paid_at = None
        self.updated_at = None
        self.callback_payload = None
        self.metadata_json = {}
        self.transaction_id = None


class StubCisPayService:
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response or {
            'id': '0198c0de-uuid',
            'order_id': 'echo',
            'status': 'PENDING',
            'amount': 50000,
            'charged_amount': 51750,
            'payment_url': 'https://pay.cispay.app/p/xyz',
            'created_at': '2026-07-19T10:00:00+00:00',
        }
        self.calls: list[dict[str, Any]] = []

    async def create_payment(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


def _make_service() -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    return service


def _enable_cispay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CISPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'CISPAY_SHOP_ID', '0198c0de-shop', raising=False)
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'cis_sec_test', raising=False)
    monkeypatch.setattr(settings, 'CISPAY_MIN_AMOUNT_KOPEKS', 10000, raising=False)
    monkeypatch.setattr(settings, 'CISPAY_MAX_AMOUNT_KOPEKS', 10000000, raising=False)


# ---------------------------------------------------------------------------
# create_cispay_payment
# ---------------------------------------------------------------------------


@pytest.mark.anyio('asyncio')
async def test_create_cispay_payment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_cispay(monkeypatch)
    stub = StubCisPayService()
    monkeypatch.setattr(cispay_mixin_module, 'cispay_service', stub)

    async def fake_get_user_by_id(_db: Any, _user_id: int) -> Any:
        class _User:
            telegram_id = 123456

        return _User()

    monkeypatch.setattr(payment_service_module, 'get_user_by_id', fake_get_user_by_id, raising=False)

    captured: dict[str, Any] = {}

    async def fake_create_cispay_payment(**kwargs: Any) -> DummyLocalPayment:
        captured.update(kwargs)
        return DummyLocalPayment(payment_id=999)

    monkeypatch.setattr(cispay_crud_module, 'create_cispay_payment', fake_create_cispay_payment)

    service = _make_service()
    result = await service.create_cispay_payment(
        db=DummySession(),
        user_id=77,
        amount_kopeks=50000,
        description='Пополнение баланса',
    )

    assert result is not None
    assert result['local_payment_id'] == 999
    assert result['payment_url'] == 'https://pay.cispay.app/p/xyz'
    assert result['amount_kopeks'] == 50000
    assert result['order_id'].startswith('cis123456_')

    api_call = stub.calls[0]
    assert api_call['amount_kopeks'] == 50000
    assert api_call['payment_method'] == 'CARD'  # дефолт без sub-метода
    assert api_call['customer_id'] == '123456'
    assert captured['payment_method'] == 'CARD'
    assert captured['charged_amount_kopeks'] == 51750


@pytest.mark.anyio('asyncio')
async def test_create_cispay_payment_sbp_sub_method(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_cispay(monkeypatch)
    stub = StubCisPayService()
    monkeypatch.setattr(cispay_mixin_module, 'cispay_service', stub)

    async def fake_get_user_by_id(_db: Any, _user_id: int) -> None:
        return None

    monkeypatch.setattr(payment_service_module, 'get_user_by_id', fake_get_user_by_id, raising=False)

    async def fake_create_cispay_payment(**_kwargs: Any) -> DummyLocalPayment:
        return DummyLocalPayment()

    monkeypatch.setattr(cispay_crud_module, 'create_cispay_payment', fake_create_cispay_payment)

    service = _make_service()
    result = await service.create_cispay_payment(
        db=DummySession(),
        user_id=77,
        amount_kopeks=50000,
        payment_method_type='sbp',
    )

    assert result is not None
    assert stub.calls[0]['payment_method'] == 'SBP'


@pytest.mark.anyio('asyncio')
async def test_create_cispay_payment_respects_amount_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_cispay(monkeypatch)
    monkeypatch.setattr(cispay_mixin_module, 'cispay_service', StubCisPayService())

    service = _make_service()
    result_low = await service.create_cispay_payment(db=DummySession(), user_id=77, amount_kopeks=9999)
    result_high = await service.create_cispay_payment(db=DummySession(), user_id=77, amount_kopeks=10000001)

    assert result_low is None
    assert result_high is None


@pytest.mark.anyio('asyncio')
async def test_create_cispay_payment_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CISPAY_ENABLED', False, raising=False)

    service = _make_service()
    result = await service.create_cispay_payment(db=DummySession(), user_id=77, amount_kopeks=50000)

    assert result is None


# ---------------------------------------------------------------------------
# process_cispay_callback
# ---------------------------------------------------------------------------


def _patch_callback_crud(monkeypatch: pytest.MonkeyPatch, payment: FakeCisPayPayment) -> AsyncMock:
    async def fake_get_by_order_id(_db: Any, _order_id: str) -> FakeCisPayPayment:
        return payment

    async def fake_get_for_update(_db: Any, _payment_id: int) -> FakeCisPayPayment:
        return payment

    update_mock = AsyncMock(return_value=payment)
    monkeypatch.setattr(cispay_crud_module, 'get_cispay_payment_by_order_id', fake_get_by_order_id)
    monkeypatch.setattr(cispay_crud_module, 'get_cispay_payment_by_id_for_update', fake_get_for_update)
    monkeypatch.setattr(cispay_crud_module, 'update_cispay_payment_status', update_mock)
    return update_mock


def _paid_webhook_payload(amount: int = 50000) -> dict[str, Any]:
    return {
        'id': '0198c0de-uuid',
        'store_id': '0198c0de-shop',
        'order_id': 'cis123_abc123',
        'payment_method': 'CARD',
        'status': 'PAID',
        'amount': amount,
        'currency': 'RUB',
        'charged_amount': 51750,
        'merchant_revenue': 48250,
        'paid_at': '2026-07-19T10:00:00+00:00',
        'timestamp': '2026-07-19T10:00:01+00:00',
    }


@pytest.mark.anyio('asyncio')
async def test_process_cispay_callback_paid_finalizes(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakeCisPayPayment()
    _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_cispay_payment', finalize_mock, raising=False)

    result = await service.process_cispay_callback(DummySession(), _paid_webhook_payload())

    assert result is True
    assert payment.is_paid is True
    assert payment.status == 'success'
    assert payment.cispay_payment_id == '0198c0de-uuid'
    assert payment.charged_amount_kopeks == 51750
    finalize_mock.assert_awaited_once()


@pytest.mark.anyio('asyncio')
async def test_process_cispay_callback_amount_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakeCisPayPayment(amount_kopeks=50000)
    update_mock = _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_cispay_payment', finalize_mock, raising=False)

    result = await service.process_cispay_callback(DummySession(), _paid_webhook_payload(amount=49999))

    assert result is False
    finalize_mock.assert_not_awaited()
    assert update_mock.await_args.kwargs['status'] == 'amount_mismatch'
    assert update_mock.await_args.kwargs['is_paid'] is False


@pytest.mark.anyio('asyncio')
async def test_process_cispay_callback_already_paid_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakeCisPayPayment(status='success', is_paid=True)
    _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_cispay_payment', finalize_mock, raising=False)

    result = await service.process_cispay_callback(DummySession(), _paid_webhook_payload())

    assert result is True
    finalize_mock.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_process_cispay_callback_sticky_terminal_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Провайдер не может «починить» отклонённый платёж повторным вебхуком."""
    payment = FakeCisPayPayment(status='declined')
    update_mock = _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_cispay_payment', finalize_mock, raising=False)

    result = await service.process_cispay_callback(DummySession(), _paid_webhook_payload())

    assert result is True
    finalize_mock.assert_not_awaited()
    update_mock.assert_not_awaited()
    assert payment.is_paid is False


@pytest.mark.anyio('asyncio')
async def test_process_cispay_callback_missing_amount_does_not_credit(monkeypatch: pytest.MonkeyPatch) -> None:
    """PAID без amount: зачислять нечего сверять — платёж остаётся pending под ретрай."""
    payment = FakeCisPayPayment()
    update_mock = _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_cispay_payment', finalize_mock, raising=False)

    payload = _paid_webhook_payload()
    del payload['amount']
    result = await service.process_cispay_callback(DummySession(), payload)

    assert result is False  # не-2xx -> cisPay повторит вебхук
    finalize_mock.assert_not_awaited()
    update_mock.assert_not_awaited()
    assert payment.is_paid is False
    assert payment.status == 'pending'  # не терминальный — ретрай ещё может закрыть платёж


@pytest.mark.anyio('asyncio')
async def test_process_cispay_callback_unparseable_amount_is_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakeCisPayPayment()
    update_mock = _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_cispay_payment', finalize_mock, raising=False)

    payload = _paid_webhook_payload()
    payload['amount'] = 'not-a-number'
    result = await service.process_cispay_callback(DummySession(), payload)

    assert result is False
    finalize_mock.assert_not_awaited()
    assert update_mock.await_args.kwargs['status'] == 'amount_mismatch'


@pytest.mark.anyio('asyncio')
async def test_process_cispay_callback_missing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    result = await service.process_cispay_callback(DummySession(), {'id': 'x'})
    assert result is False


@pytest.mark.anyio('asyncio')
async def test_process_cispay_callback_non_paid_status_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakeCisPayPayment()
    update_mock = _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_cispay_payment', finalize_mock, raising=False)

    payload = _paid_webhook_payload()
    payload['status'] = 'EXPIRED'
    result = await service.process_cispay_callback(DummySession(), payload)

    assert result is True
    finalize_mock.assert_not_awaited()
    assert update_mock.await_args.kwargs['status'] == 'expired'


# ---------------------------------------------------------------------------
# verify_webhook_signature
# ---------------------------------------------------------------------------


@pytest.mark.anyio('asyncio')
async def test_generic_method_falls_back_to_sbp_when_card_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """SBP-only магазин: генерик-метод обязан слать SBP, иначе cisPay отклонит платёж."""
    _enable_cispay(monkeypatch)
    monkeypatch.setattr(settings, 'CISPAY_SBP_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'CISPAY_CARD_ENABLED', False, raising=False)

    stub = StubCisPayService()
    monkeypatch.setattr(cispay_mixin_module, 'cispay_service', stub)

    async def fake_get_user_by_id(_db: Any, _user_id: int) -> None:
        return None

    monkeypatch.setattr(payment_service_module, 'get_user_by_id', fake_get_user_by_id, raising=False)

    async def fake_create_cispay_payment(**_kwargs: Any) -> DummyLocalPayment:
        return DummyLocalPayment()

    monkeypatch.setattr(cispay_crud_module, 'create_cispay_payment', fake_create_cispay_payment)

    service = _make_service()
    await service.create_cispay_payment(db=DummySession(), user_id=77, amount_kopeks=50000)

    assert stub.calls[0]['payment_method'] == 'SBP'


def test_is_cispay_enabled_rejects_blank_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустая строка ключа не должна включать шлюз — иначе HMAC вебхука подделывается."""
    monkeypatch.setattr(settings, 'CISPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'CISPAY_SHOP_ID', 'shop', raising=False)
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', '', raising=False)

    assert settings.is_cispay_enabled() is False

    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'cis_sec_test', raising=False)
    assert settings.is_cispay_enabled() is True


def test_verify_webhook_signature_blank_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', '', raising=False)
    service = CisPayService()

    raw_body = b'{"order_id":"cis1_x","status":"PAID","amount":50000}'
    forged = hmac.new(b'', raw_body, hashlib.sha256).hexdigest()

    assert service.verify_webhook_signature(raw_body, forged) is False


def test_verify_webhook_signature_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'cis_sec_test', raising=False)
    service = CisPayService()

    raw_body = b'{"order_id":"cis1_x","status":"PAID","amount":50000}'
    signature = hmac.new(b'cis_sec_test', raw_body, hashlib.sha256).hexdigest()

    assert service.verify_webhook_signature(raw_body, signature) is True
    # Регистр hex-подписи не важен
    assert service.verify_webhook_signature(raw_body, signature.upper()) is True


def test_verify_webhook_signature_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'cis_sec_test', raising=False)
    service = CisPayService()

    raw_body = b'{"order_id":"cis1_x","status":"PAID","amount":50000}'
    wrong = hmac.new(b'other_key', raw_body, hashlib.sha256).hexdigest()

    assert service.verify_webhook_signature(raw_body, wrong) is False
    assert service.verify_webhook_signature(raw_body, None) is False
    assert service.verify_webhook_signature(raw_body, '') is False


def test_verify_webhook_signature_tampered_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'cis_sec_test', raising=False)
    service = CisPayService()

    raw_body = b'{"order_id":"cis1_x","status":"PAID","amount":50000}'
    signature = hmac.new(b'cis_sec_test', raw_body, hashlib.sha256).hexdigest()
    tampered = raw_body.replace(b'50000', b'99999')

    assert service.verify_webhook_signature(tampered, signature) is False
