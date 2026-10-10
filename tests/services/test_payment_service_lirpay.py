"""Тесты для сценариев LirPay в PaymentService.

Покрывают создание payment link (выбор способа, лимиты сумм, idempotency
key = order_id), обработку вебхука (зачисление, тестовый счёт, несовпадение
суммы, идемпотентность, терминальные статусы, события не по платежам) и
проверку HMAC-подписи вебхука.

Отдельно зафиксированы отличия LirPay от соседних адаптеров: тестовое
окружение оплачивает эмулятором и НЕ начисляет баланс; способ не
подставляется по умолчанию; наш order_id в теле вебхука отсутствует —
матчинг идёт по public_id и по customer_id+сумме; секрет вебхука выдаётся
отдельно от ключей API.
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

import app.database.crud.lirpay as lirpay_crud_module
from app.config import settings
from app.services.lirpay_service import LirPayService, amount_to_kopeks, kopeks_to_amount
from app.services.payment.lirpay import LIRPAY_FINAL_STATUSES
from app.services.payment_service import PaymentService


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


class _EmptyScalars:
    def all(self):
        return []

    def scalar_one_or_none(self):
        return None


class _EmptyResult:
    def scalars(self):
        return _EmptyScalars()


class DummySession:
    async def commit(self) -> None:
        return None

    async def refresh(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        # фолбэк-матчинг вебхука: пустой набор кандидатов
        return _EmptyResult()


class DummyLocalPayment:
    def __init__(self, payment_id: int = 501) -> None:
        self.id = payment_id
        self.created_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class FakeLirPayPayment:
    def __init__(
        self,
        *,
        status: str = 'pending',
        is_paid: bool = False,
        amount_kopeks: int = 50000,
        currency: str = 'RUB',
    ) -> None:
        self.id = 11
        self.user_id = 77
        self.order_id = 'lp123456_ab12cd'
        self.lirpay_payment_id = 'a1b2c3d4e5f67890'
        self.amount_kopeks = amount_kopeks
        self.currency = currency
        self.payment_method = None
        self.status = status
        self.is_paid = is_paid
        self.paid_at = None
        self.updated_at = None
        self.callback_payload = None
        self.metadata_json = {'customer_id': '123456'}
        self.transaction_id = None


class StubLirPayService:
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response or {
            'public_id': 'a1b2c3d4e5f67890',
            'payment_link': 'https://lirpay.org/pay/a1b2c3d4e5f67890',
        }
        self.calls: list[dict[str, Any]] = []

    async def create_payment_link(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


def _make_service() -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    return service


def _enable_lirpay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'LIRPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'LIRPAY_PUBLIC_KEY', 'lpk_live_test', raising=False)
    monkeypatch.setattr(settings, 'LIRPAY_SECRET_KEY', 'lsk_live_test', raising=False)
    monkeypatch.setattr(settings, 'LIRPAY_WEBHOOK_SECRET', 'whsec_test', raising=False)
    monkeypatch.setattr(settings, 'LIRPAY_PROJECT_ID', '3fa85f64-5717-4562-b3fc-2c963f66afa6', raising=False)
    monkeypatch.setattr(settings, 'LIRPAY_MIN_AMOUNT_KOPEKS', 10000, raising=False)
    monkeypatch.setattr(settings, 'LIRPAY_MAX_AMOUNT_KOPEKS', 10000000, raising=False)
    monkeypatch.setattr(settings, 'LIRPAY_CURRENCY', 'RUB', raising=False)


# ---------------------------------------------------------------------------
# Конвертация сумм
# ---------------------------------------------------------------------------


def test_kopeks_to_amount_roundtrip() -> None:
    assert kopeks_to_amount(125000) == '1250.00'
    assert kopeks_to_amount(1005) == '10.05'
    assert kopeks_to_amount(10) == '0.10'
    for kopeks in (1, 99, 100, 10_05, 1_234_567):
        assert amount_to_kopeks(kopeks_to_amount(kopeks)) == kopeks


def test_amount_to_kopeks_unparseable() -> None:
    assert amount_to_kopeks(None) is None
    assert amount_to_kopeks('abc') is None
    assert amount_to_kopeks(True) is None
    assert amount_to_kopeks(1250.005) is None  # доли копейки


# ---------------------------------------------------------------------------
# Создание платежа
# ---------------------------------------------------------------------------


async def test_create_payment_passes_idempotency_and_customer(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_lirpay(monkeypatch)
    stub = StubLirPayService()
    monkeypatch.setattr('app.services.payment.lirpay.lirpay_service', stub)

    payment_module = type(sys)('app.services.payment_service')
    payment_module.get_user_by_id = AsyncMock(return_value=type('U', (), {'telegram_id': 123456})())
    monkeypatch.setitem(sys.modules, 'app.services.payment_service', payment_module)
    monkeypatch.setattr(
        'app.database.crud.lirpay.create_lirpay_payment',
        AsyncMock(return_value=DummyLocalPayment()),
    )

    service = _make_service()
    result = await service.create_lirpay_payment(
        DummySession(),
        user_id=1,
        amount_kopeks=50000,
        description='Пополнение баланса',
    )

    assert result is not None
    assert result['payment_url'] == 'https://lirpay.org/pay/a1b2c3d4e5f67890'
    assert result['payment_id'] == 'a1b2c3d4e5f67890'
    assert result['amount_rubles'] == 500.0

    call = stub.calls[0]
    # Idempotency-Key = наш order_id: повтор запроса не создаст второй счёт.
    assert call['idempotency_key'] == result['order_id']
    assert call['amount_kopeks'] == 50000
    assert call['project_id'] == settings.LIRPAY_PROJECT_ID
    assert call['customer_id'] == '123456'
    assert 'method' not in call  # method_mode=multi: способ выбирает покупатель на странице


async def test_create_payment_below_min(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_lirpay(monkeypatch)
    service = _make_service()
    result = await service.create_lirpay_payment(DummySession(), user_id=1, amount_kopeks=1000)
    assert result is None


async def test_create_payment_above_max(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_lirpay(monkeypatch)
    service = _make_service()
    result = await service.create_lirpay_payment(DummySession(), user_id=1, amount_kopeks=20_000_000)
    assert result is None


async def test_create_payment_without_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_lirpay(monkeypatch)
    monkeypatch.setattr(settings, 'LIRPAY_PROJECT_ID', '', raising=False)
    service = _make_service()
    result = await service.create_lirpay_payment(DummySession(), user_id=1, amount_kopeks=50000)
    assert result is None


# ---------------------------------------------------------------------------
# Вебхук
# ---------------------------------------------------------------------------


def _webhook_body(
    *,
    event: str = 'payment.succeeded',
    public_id: str = 'a1b2c3d4e5f67890',
    amount: str = '500.00',
    customer_id: str | None = '123456',
    status: str = 'paid',
) -> dict[str, Any]:
    body: dict[str, Any] = {
        'type': event,
        'payment': {
            'public_id': public_id,
            'amount': amount,
            'status': status,
        },
    }
    if customer_id is not None:
        body['payment']['customer_id'] = customer_id
    return body


async def test_webhook_success_credits_balance(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_lirpay(monkeypatch)
    payment = FakeLirPayPayment()

    async def fake_get_by_order_id(db, order_id):
        return payment

    async def fake_get_by_invoice_id(db, invoice_id):
        return payment

    monkeypatch.setattr(lirpay_crud_module, 'get_lirpay_payment_by_order_id', fake_get_by_order_id)
    monkeypatch.setattr(lirpay_crud_module, 'get_lirpay_payment_by_invoice_id', fake_get_by_invoice_id)

    async def fake_get_for_update(db, payment_id):
        return payment

    monkeypatch.setattr(lirpay_crud_module, 'get_lirpay_payment_by_id_for_update', fake_get_for_update)

    finalize_calls = []

    async def fake_finalize(db, payment_obj, *, trigger):
        finalize_calls.append(trigger)
        return True

    service = _make_service()
    monkeypatch.setattr(service, '_finalize_lirpay_payment', fake_finalize)

    result = await service.process_lirpay_callback(DummySession(), _webhook_body())
    assert result is True
    assert payment.is_paid is True
    assert payment.status == 'success'
    assert payment.paid_at is not None
    assert finalize_calls == ['webhook']


async def test_webhook_test_mode_never_credits(monkeypatch: pytest.MonkeyPatch) -> None:
    """TEST-ключ «оплачивает» эмулятором без денег — баланс не начисляем."""
    _enable_lirpay(monkeypatch)
    payment = FakeLirPayPayment()
    body = _webhook_body()
    body['_lirpay_mode'] = 'test'

    monkeypatch.setattr(
        lirpay_crud_module,
        'get_lirpay_payment_by_invoice_id',
        AsyncMock(return_value=payment),
    )
    monkeypatch.setattr(
        lirpay_crud_module,
        'get_lirpay_payment_by_id_for_update',
        AsyncMock(return_value=payment),
    )

    async def fake_update(db, payment, **kw):
        payment.status = kw.get('status', payment.status)
        if kw.get('is_paid') is not None:
            payment.is_paid = kw['is_paid']
        return payment

    monkeypatch.setattr(lirpay_crud_module, 'update_lirpay_payment_status', fake_update)

    service = _make_service()
    result = await service.process_lirpay_callback(DummySession(), body)
    assert result is True
    assert payment.is_paid is False
    assert payment.status == 'error'


async def test_webhook_amount_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_lirpay(monkeypatch)
    payment = FakeLirPayPayment(amount_kopeks=50000)
    body = _webhook_body(amount='600.00')

    monkeypatch.setattr(
        lirpay_crud_module,
        'get_lirpay_payment_by_invoice_id',
        AsyncMock(return_value=payment),
    )
    monkeypatch.setattr(
        lirpay_crud_module,
        'get_lirpay_payment_by_id_for_update',
        AsyncMock(return_value=payment),
    )
    updated = []

    async def fake_update(db, payment, **kw):
        payment.status = kw.get('status', payment.status)
        updated.append(kw.get('status'))
        return payment

    monkeypatch.setattr(lirpay_crud_module, 'update_lirpay_payment_status', fake_update)

    service = _make_service()
    result = await service.process_lirpay_callback(DummySession(), body)
    assert result is False
    assert payment.is_paid is False
    assert updated == ['amount_mismatch']


async def test_webhook_without_amount_not_credited(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_lirpay(monkeypatch)
    payment = FakeLirPayPayment()
    body = _webhook_body(amount=None)

    monkeypatch.setattr(
        lirpay_crud_module,
        'get_lirpay_payment_by_invoice_id',
        AsyncMock(return_value=payment),
    )
    monkeypatch.setattr(
        lirpay_crud_module,
        'get_lirpay_payment_by_id_for_update',
        AsyncMock(return_value=payment),
    )

    service = _make_service()
    result = await service.process_lirpay_callback(DummySession(), body)
    assert result is False
    assert payment.is_paid is False


async def test_webhook_already_paid_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_lirpay(monkeypatch)
    payment = FakeLirPayPayment(is_paid=True, status='success')

    monkeypatch.setattr(
        lirpay_crud_module,
        'get_lirpay_payment_by_invoice_id',
        AsyncMock(return_value=payment),
    )
    monkeypatch.setattr(
        lirpay_crud_module,
        'get_lirpay_payment_by_id_for_update',
        AsyncMock(return_value=payment),
    )

    service = _make_service()
    result = await service.process_lirpay_callback(DummySession(), _webhook_body())
    assert result is True


async def test_webhook_unknown_payment_acked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Чужой счёт повторами не появится — подтверждаем доставку."""
    _enable_lirpay(monkeypatch)
    monkeypatch.setattr(
        lirpay_crud_module,
        'get_lirpay_payment_by_order_id',
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        lirpay_crud_module,
        'get_lirpay_payment_by_invoice_id',
        AsyncMock(return_value=None),
    )

    service = _make_service()
    result = await service.process_lirpay_callback(DummySession(), _webhook_body())
    assert result is True


async def test_webhook_non_payment_event_acked(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_lirpay(monkeypatch)
    service = _make_service()
    result = await service.process_lirpay_callback(DummySession(), {'type': 'balance.updated'})
    assert result is True


async def test_webhook_expired_marks_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_lirpay(monkeypatch)
    payment = FakeLirPayPayment()
    updated = []

    async def fake_update(db, payment, **kw):
        payment.status = kw.get('status', payment.status)
        updated.append(kw.get('status'))
        return payment

    monkeypatch.setattr(
        lirpay_crud_module,
        'get_lirpay_payment_by_invoice_id',
        AsyncMock(return_value=payment),
    )
    monkeypatch.setattr(
        lirpay_crud_module,
        'get_lirpay_payment_by_id_for_update',
        AsyncMock(return_value=payment),
    )
    monkeypatch.setattr(lirpay_crud_module, 'update_lirpay_payment_status', fake_update)

    service = _make_service()
    result = await service.process_lirpay_callback(
        DummySession(), _webhook_body(event='payment.expired', status='expired')
    )
    assert result is True
    assert updated == ['expired']
    assert 'expired' not in LIRPAY_FINAL_STATUSES  # поздняя оплата всё ещё зачислится


async def test_webhook_refund_requires_manual_review(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_lirpay(monkeypatch)
    payment = FakeLirPayPayment(is_paid=True, status='success')
    updated = []

    async def fake_update(db, payment, **kw):
        payment.status = kw.get('status', payment.status)
        updated.append(kw.get('status'))
        return payment

    monkeypatch.setattr(
        lirpay_crud_module,
        'get_lirpay_payment_by_invoice_id',
        AsyncMock(return_value=payment),
    )
    monkeypatch.setattr(
        lirpay_crud_module,
        'get_lirpay_payment_by_id_for_update',
        AsyncMock(return_value=payment),
    )
    monkeypatch.setattr(lirpay_crud_module, 'update_lirpay_payment_status', fake_update)

    service = _make_service()
    result = await service.process_lirpay_callback(DummySession(), _webhook_body(event='payment.refunded'))
    assert result is True
    assert updated == ['refunded']


# ---------------------------------------------------------------------------
# Подпись вебхука
# ---------------------------------------------------------------------------


def _sign(raw: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def test_webhook_signature_ok() -> None:
    service = LirPayService()
    service._webhook_secret = 'whsec_test'
    raw = b'{"type":"payment.succeeded"}'
    assert service.verify_webhook_signature(raw, _sign(raw, 'whsec_test')) is True
    # с префиксом sha256= тоже принимаем
    assert service.verify_webhook_signature(raw, f'sha256={_sign(raw, "whsec_test")}') is True


def test_webhook_signature_bad() -> None:
    service = LirPayService()
    service._webhook_secret = 'whsec_test'
    raw = b'{"type":"payment.succeeded"}'
    assert service.verify_webhook_signature(raw, _sign(raw, 'other')) is False
    assert service.verify_webhook_signature(raw, '') is False
    assert service.verify_webhook_signature(raw, None) is False


def test_webhook_signature_no_secret() -> None:
    """Без секрета HMAC считался бы от известного тела — подпись подделал бы кто угодно."""
    service = LirPayService()
    service._webhook_secret = ''
    raw = b'{}'
    assert service.verify_webhook_signature(raw, _sign(raw, '')) is False


def test_webhook_signature_uses_raw_body() -> None:
    """Подпись считается от сырого тела, а не от пере-сериализованного JSON."""
    service = LirPayService()
    service._webhook_secret = 'whsec_test'
    raw = b'{"type": "payment.succeeded", "amount": "500.00"}'
    # тот же JSON, другая сериализация
    assert (
        service.verify_webhook_signature(b'{"amount":"500.00","type":"payment.succeeded"}', _sign(raw, 'whsec_test'))
        is False
    )


# ---------------------------------------------------------------------------
# Тестовое окружение: сверка не начисляет эмуляторные оплаты
# ---------------------------------------------------------------------------


def test_is_test_key_detects_prefix() -> None:
    """Среда определяется ключом: lpk_test_ — песочница, lpk_live_ — бой."""
    service = LirPayService()
    service._public_key = 'lpk_test_cec2667b9b5880ac'
    assert service.is_test_key() is True
    service._public_key = 'lpk_live_eb8c23abb59d1b60'
    assert service.is_test_key() is False


async def test_api_check_with_test_key_never_credits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сверка по тестовому ключу видит paid, но баланс не начисляет.

    Ответ GET /payment-links режима не несёт — раньше тестовые «оплаты»
    эмулятором зачислялись молча; теперь статус error и запись в лог.
    """
    _enable_lirpay(monkeypatch)
    monkeypatch.setattr(settings, 'LIRPAY_PUBLIC_KEY', 'lpk_test_cec2667b9b5880ac', raising=False)

    payment = FakeLirPayPayment()
    payment.lirpay_payment_id = 'a1b2c3d4e5f67890'

    async def fake_get_by_order_id(db, order_id):
        return payment

    async def fake_update(db, payment, **kw):
        payment.status = kw.get('status', payment.status)
        if kw.get('is_paid') is not None:
            payment.is_paid = kw['is_paid']
        return payment

    monkeypatch.setattr(lirpay_crud_module, 'get_lirpay_payment_by_order_id', fake_get_by_order_id)
    monkeypatch.setattr(lirpay_crud_module, 'update_lirpay_payment_status', fake_update)

    class StubLinkService:
        is_test = True

        def is_test_key(self):
            return self.is_test

        async def get_payment_link(self, public_id):
            return {
                'public_id': public_id,
                'status': 'paid',
                'amount': '500.00',
                'currency': 'RUB',
            }

    monkeypatch.setattr('app.services.payment.lirpay.lirpay_service', StubLinkService())

    service = _make_service()
    result = await service.check_lirpay_payment_status(DummySession(), payment.order_id)
    assert result is not None
    assert result['status'] == 'error'
    assert payment.is_paid is False
    assert payment.status == 'error'


# ---------------------------------------------------------------------------
# Аудит-фиксы: блокировка, неоднозначный матч, валюта, поздние события
# ---------------------------------------------------------------------------


async def test_webhook_takes_for_update_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Вебхук обязан брать FOR UPDATE до применения события."""
    _enable_lirpay(monkeypatch)
    payment = FakeLirPayPayment()
    lock_calls = []

    async def fake_get_by_invoice_id(db, invoice_id):
        return payment

    async def fake_get_for_update(db, payment_id):
        lock_calls.append(payment_id)
        return payment

    monkeypatch.setattr(lirpay_crud_module, 'get_lirpay_payment_by_invoice_id', fake_get_by_invoice_id)
    monkeypatch.setattr(lirpay_crud_module, 'get_lirpay_payment_by_id_for_update', fake_get_for_update)

    async def fake_finalize(db, payment_obj, *, trigger):
        return True

    service = _make_service()
    monkeypatch.setattr(service, '_finalize_lirpay_payment', fake_finalize)
    result = await service.process_lirpay_callback(DummySession(), _webhook_body())
    assert result is True
    assert lock_calls == [payment.id]


async def test_webhook_ambiguous_fallback_nacks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Неоднозначный матч по customer_id+сумме — повторная доставка, не ACK."""

    class FakeRows:
        def scalars(self):
            class S:
                def all(self):
                    return [FakeLirPayPayment(), FakeLirPayPayment()]

            return S()

    class FakeExecSession(DummySession):
        async def execute(self, *_a, **_kw):
            return FakeRows()

    monkeypatch.setattr(lirpay_crud_module, 'get_lirpay_payment_by_order_id', AsyncMock(return_value=None))
    monkeypatch.setattr(lirpay_crud_module, 'get_lirpay_payment_by_invoice_id', AsyncMock(return_value=None))

    service = _make_service()
    result = await service.process_lirpay_callback(FakeExecSession(), _webhook_body())
    assert result is False


async def test_webhook_currency_mismatch_not_credited(monkeypatch: pytest.MonkeyPatch) -> None:
    """Счёт в USD не зачисляется как рубли."""
    _enable_lirpay(monkeypatch)
    payment = FakeLirPayPayment(amount_kopeks=50000, currency='RUB')
    body = _webhook_body(amount='500.00')
    body['payment']['currency'] = 'USD'

    monkeypatch.setattr(lirpay_crud_module, 'get_lirpay_payment_by_invoice_id', AsyncMock(return_value=payment))
    monkeypatch.setattr(lirpay_crud_module, 'get_lirpay_payment_by_id_for_update', AsyncMock(return_value=payment))
    updated = []

    async def fake_update(db, **kw):
        payment_obj = kw.get('payment')
        if payment_obj is not None:
            payment_obj.status = kw.get('status', payment_obj.status)
        updated.append(kw.get('status'))
        return payment_obj

    monkeypatch.setattr(lirpay_crud_module, 'update_lirpay_payment_status', fake_update)

    service = _make_service()
    result = await service.process_lirpay_callback(DummySession(), body)
    assert result is False
    assert payment.is_paid is False
    assert updated == ['amount_mismatch']


async def test_webhook_late_expired_does_not_touch_paid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Поздний expired после зачисления не перезаписывает оплаченный платёж."""
    _enable_lirpay(monkeypatch)
    payment = FakeLirPayPayment(is_paid=True, status='success')

    monkeypatch.setattr(lirpay_crud_module, 'get_lirpay_payment_by_invoice_id', AsyncMock(return_value=payment))
    monkeypatch.setattr(lirpay_crud_module, 'get_lirpay_payment_by_id_for_update', AsyncMock(return_value=payment))
    update_calls = []

    async def fake_update(db, **kw):
        update_calls.append(kw.get('status'))
        return kw.get('payment')

    monkeypatch.setattr(lirpay_crud_module, 'update_lirpay_payment_status', fake_update)

    service = _make_service()
    result = await service.process_lirpay_callback(
        DummySession(), _webhook_body(event='payment.expired', status='expired')
    )
    assert result is True
    assert update_calls == [], 'оплаченный платёж не должен менять статус'
    assert payment.status == 'success'
    assert payment.is_paid is True


async def test_webhook_repeat_succeeded_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Повторная доставка succeeded не зачисляет дважды (гвард is_paid)."""
    _enable_lirpay(monkeypatch)
    payment = FakeLirPayPayment(is_paid=True, status='success')

    monkeypatch.setattr(lirpay_crud_module, 'get_lirpay_payment_by_invoice_id', AsyncMock(return_value=payment))
    monkeypatch.setattr(lirpay_crud_module, 'get_lirpay_payment_by_id_for_update', AsyncMock(return_value=payment))

    finalize_calls = []

    async def fake_finalize(db, payment_obj, *, trigger):
        finalize_calls.append(trigger)
        return True

    service = _make_service()
    monkeypatch.setattr(service, '_finalize_lirpay_payment', fake_finalize)
    result = await service.process_lirpay_callback(DummySession(), _webhook_body())
    assert result is True
    assert finalize_calls == []


async def test_api_check_credits_production_payment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сверка доначисляет боевой платёж при потерянном вебхуке."""
    _enable_lirpay(monkeypatch)
    payment = FakeLirPayPayment()

    async def fake_get_by_order_id(db, order_id):
        return payment

    monkeypatch.setattr(lirpay_crud_module, 'get_lirpay_payment_by_order_id', fake_get_by_order_id)
    monkeypatch.setattr(lirpay_crud_module, 'get_lirpay_payment_by_id_for_update', AsyncMock(return_value=payment))

    class StubLinkService:
        def is_test_key(self):
            return False

        async def get_payment_link(self, public_id):
            return {
                'public_id': public_id,
                'status': 'paid',
                'amount': '500.00',
                'currency': 'RUB',
            }

    monkeypatch.setattr('app.services.payment.lirpay.lirpay_service', StubLinkService())

    async def fake_finalize(db, payment_obj, *, trigger):
        payment_obj.is_paid = True
        payment_obj.status = 'success'
        return True

    service = _make_service()
    monkeypatch.setattr(service, '_finalize_lirpay_payment', fake_finalize)
    result = await service.check_lirpay_payment_status(DummySession(), payment.order_id)
    assert result is not None
    assert result['status'] == 'success'
    assert payment.is_paid is True
