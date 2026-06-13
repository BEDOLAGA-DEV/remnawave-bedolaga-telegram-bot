"""Тесты для сценариев MulenPay в PaymentService."""

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
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
    async def commit(self) -> None:  # pragma: no cover - метод вызывается, но без логики
        return None

    async def refresh(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def flush(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class DummyLocalPayment:
    def __init__(self, payment_id: int = 501) -> None:
        self.id = payment_id
        self.created_at = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


class StubMulenPayService:
    def __init__(self, response: dict[str, Any] | None) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def create_payment(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(kwargs)
        return self.response


def _make_service(stub: StubMulenPayService | None) -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    service.mulenpay_service = stub
    service.pal24_service = None
    service.yookassa_service = None
    service.stars_service = None
    service.cryptobot_service = None
    service.heleket_service = None
    return service


@pytest.mark.anyio('asyncio')
async def test_create_mulenpay_payment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    response = {'id': 123, 'paymentUrl': 'https://mulenpay/pay'}
    stub = StubMulenPayService(response)
    service = _make_service(stub)
    db = DummySession()

    captured_args: dict[str, Any] = {}

    async def fake_create_mulenpay_payment(**kwargs: Any) -> DummyLocalPayment:
        captured_args.update(kwargs)
        return DummyLocalPayment(payment_id=999)

    monkeypatch.setattr(
        payment_service_module,
        'create_mulenpay_payment',
        fake_create_mulenpay_payment,
        raising=False,
    )
    monkeypatch.setattr(settings, 'MULENPAY_MIN_AMOUNT_KOPEKS', 1000, raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_MAX_AMOUNT_KOPEKS', 1_000_000, raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_VAT_CODE', 1, raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_PAYMENT_SUBJECT', 'service', raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_PAYMENT_MODE', 'full_payment', raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_LANGUAGE', 'ru', raising=False)
    monkeypatch.setattr(settings, 'WEBHOOK_URL', 'https://example.com', raising=False)

    result = await service.create_mulenpay_payment(
        db=db,
        user_id=77,
        amount_kopeks=25000,
        description='Пополнение',
        language='en',
    )

    assert result is not None
    assert result['local_payment_id'] == 999
    assert result['mulen_payment_id'] == 123
    assert result['payment_url'] == 'https://mulenpay/pay'
    assert result['status'] == 'created'
    assert stub.calls and stub.calls[0]['language'] == 'en'
    assert captured_args['user_id'] == 77
    assert captured_args['amount_kopeks'] == 25000
    assert captured_args['uuid'].startswith('mulen_77_')


@pytest.mark.anyio('asyncio')
async def test_create_mulenpay_payment_respects_amount_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubMulenPayService({'id': 1})
    service = _make_service(stub)
    db = DummySession()

    monkeypatch.setattr(settings, 'MULENPAY_MIN_AMOUNT_KOPEKS', 5000, raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_MAX_AMOUNT_KOPEKS', 10_000, raising=False)

    result_low = await service.create_mulenpay_payment(
        db=db,
        user_id=1,
        amount_kopeks=1000,
        description='Пополнение',
    )
    assert result_low is None

    result_high = await service.create_mulenpay_payment(
        db=db,
        user_id=1,
        amount_kopeks=20_000,
        description='Пополнение',
    )
    assert result_high is None
    assert not stub.calls


@pytest.mark.anyio('asyncio')
async def test_create_mulenpay_payment_returns_none_without_service() -> None:
    service = _make_service(None)
    db = DummySession()

    result = await service.create_mulenpay_payment(
        db=db,
        user_id=1,
        amount_kopeks=5000,
        description='Пополнение',
    )
    assert result is None


@pytest.mark.anyio('asyncio')
async def test_process_mulenpay_callback_avoids_duplicate_transactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(None)
    db = DummySession()

    class DummyPayment:
        def __init__(self) -> None:
            self.id = 501
            self.user_id = 42
            self.amount_kopeks = 1500
            self.description = 'Пополнение'
            self.uuid = 'mulen_1_test'
            self.transaction_id: int | None = None
            self.mulen_payment_id: int | None = None
            self.status = 'created'
            self.is_paid = False
            self.paid_at: datetime | None = None
            self.callback_payload: dict[str, Any] | None = None
            self.metadata_json: dict[str, Any] = {}
            self.created_at = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
            self.updated_at = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)

    payment = DummyPayment()

    # Production locks the payment row FOR UPDATE before mutating it
    # (TOCTOU guard added in the v3.54->v3.57 merge). The lock CRUD is
    # imported fresh from app.database.crud.mulenpay at call time, so it
    # must be patched on the real module rather than payment_service_module.
    async def fake_get_mulenpay_payment_by_id_for_update(
        _db: DummySession, payment_id: int
    ) -> DummyPayment:
        assert payment_id == payment.id
        return payment

    async def fake_get_mulenpay_payment_by_uuid(_db: DummySession, uuid: str) -> DummyPayment:
        assert uuid == payment.uuid
        return payment

    async def fake_update_mulenpay_payment_status(_db: DummySession, **kwargs: Any) -> DummyPayment:
        payment.status = kwargs.get('status', payment.status)
        payment.mulen_payment_id = kwargs.get('mulen_payment_id', payment.mulen_payment_id)
        return payment

    transaction_calls: list[dict[str, Any]] = []

    class DummyTransaction:
        def __init__(self, transaction_id: int = 555) -> None:
            self.id = transaction_id

    async def fake_create_transaction(_db: DummySession, **kwargs: Any) -> DummyTransaction:
        transaction_calls.append(kwargs)
        return DummyTransaction()

    async def fake_link_payment(db: DummySession, *, payment: DummyPayment, transaction_id: int) -> DummyPayment:
        payment.transaction_id = transaction_id
        return payment

    class DummyUser:
        def __init__(self) -> None:
            self.id = payment.user_id
            self.telegram_id = 99
            self.balance_kopeks = 0
            self.has_made_first_topup = False
            self.language = 'ru'
            self.promo_group = None
            self.subscription = None
            self.user_promo_groups = []
            self.referred_by_id = None

        def get_primary_promo_group(self):
            return self.promo_group

    dummy_user = DummyUser()

    async def fake_get_user_by_id(_db: DummySession, user_id: int) -> DummyUser:
        assert user_id == payment.user_id
        return dummy_user

    async def fake_process_referral_topup(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_auto_purchase_saved_cart_after_topup(*_args: Any, **_kwargs: Any) -> bool:
        return False

    async def fake_try_auto_extend_expired_after_topup(*_args: Any, **_kwargs: Any) -> bool:
        return False

    async def fake_try_resume_disabled_daily_after_topup(*_args: Any, **_kwargs: Any) -> bool:
        return False

    async def fake_has_user_cart(*_args: Any, **_kwargs: Any) -> bool:
        return False

    async def fake_get_user_cart(*_args: Any, **_kwargs: Any) -> None:
        return None

    referral_module = ModuleType('app.services.referral_service')
    referral_module.process_referral_topup = fake_process_referral_topup  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, 'app.services.referral_service', referral_module)

    # send_cart_notification_after_topup (run on the success path after commit)
    # imports three helpers from this module, so the stub must expose all of them
    # or the helper raises ImportError (swallowed by production, but it pollutes
    # the path we are asserting on).
    auto_module = ModuleType('app.services.subscription_auto_purchase_service')
    auto_module.auto_purchase_saved_cart_after_topup = (  # type: ignore[attr-defined]
        fake_auto_purchase_saved_cart_after_topup
    )
    auto_module.try_auto_extend_expired_after_topup = (  # type: ignore[attr-defined]
        fake_try_auto_extend_expired_after_topup
    )
    auto_module.try_resume_disabled_daily_after_topup = (  # type: ignore[attr-defined]
        fake_try_resume_disabled_daily_after_topup
    )
    monkeypatch.setitem(sys.modules, 'app.services.subscription_auto_purchase_service', auto_module)

    user_cart_module = ModuleType('app.services.user_cart_service')
    user_cart_module.user_cart_service = SimpleNamespace(  # type: ignore[attr-defined]
        has_user_cart=fake_has_user_cart,
        get_user_cart=fake_get_user_cart,
    )
    monkeypatch.setitem(sys.modules, 'app.services.user_cart_service', user_cart_module)

    # Patch the lock CRUD on its real module — production imports it fresh via
    # import_module('app.database.crud.mulenpay') at call time.
    import app.database.crud.mulenpay as mulenpay_crud_module

    monkeypatch.setattr(
        mulenpay_crud_module,
        'get_mulenpay_payment_by_id_for_update',
        fake_get_mulenpay_payment_by_id_for_update,
        raising=False,
    )

    # Production also FOR-UPDATE-locks the user row before crediting balance
    # (race guard added in the v3.54->v3.57 merge); the lock CRUD is imported
    # locally from app.database.crud.user at call time.
    async def fake_lock_user_for_update(_db: DummySession, user: DummyUser) -> DummyUser:
        return user

    import app.database.crud.user as user_crud_module

    monkeypatch.setattr(
        user_crud_module,
        'lock_user_for_update',
        fake_lock_user_for_update,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        'get_mulenpay_payment_by_uuid',
        fake_get_mulenpay_payment_by_uuid,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        'update_mulenpay_payment_status',
        fake_update_mulenpay_payment_status,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        'create_transaction',
        fake_create_transaction,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        'link_mulenpay_payment_to_transaction',
        fake_link_payment,
        raising=False,
    )
    monkeypatch.setattr(
        payment_service_module,
        'get_user_by_id',
        fake_get_user_by_id,
        raising=False,
    )

    result = await service.process_mulenpay_callback(
        db,
        {'uuid': payment.uuid, 'payment_status': 'success', 'id': 123, 'amount': 1500},
    )

    assert result is True
    # Exactly one transaction is created — the core "avoids duplicate" guarantee.
    assert len(transaction_calls) == 1, 'create_transaction should be called exactly once'
    # Production credits the balance DIRECTLY (user.balance_kopeks += amount, no
    # add_user_balance on this path), so the balance lands at exactly the payment
    # amount — credited once, never twice.
    assert dummy_user.balance_kopeks == payment.amount_kopeks
    # The single transaction is linked back to the payment, which is what guards
    # the early-return short-circuit against a duplicate transaction on replay.
    assert payment.transaction_id is not None
    assert transaction_calls[0]['amount_kopeks'] == payment.amount_kopeks
