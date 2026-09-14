"""Тесты для сценариев TabPay в PaymentService.

Покрывают создание платежа (маппинг sub-методов, лимиты сумм, восстановление
после сетевого сбоя), обработку вебхука (зачисление, несовпадение суммы,
идемпотентность по паре (id, status), поздняя оплата, тестовые вебхуки) и
проверку подписи X-Signature-V2 со свежестью метки времени.
"""

import hashlib
import hmac
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.database.crud.tabpay as tabpay_crud_module
import app.services.payment.tabpay as tabpay_mixin_module
from app.config import settings
from app.services import payment_service as payment_service_module
from app.services.payment_service import PaymentService
from app.services.tabpay_service import TabPayAPIError, TabPayNetworkError, TabPayService


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


class FakeTabPayPayment:
    def __init__(
        self,
        *,
        status: str = 'pending',
        is_paid: bool = False,
        amount_kopeks: int = 50000,
        processed_events: list[str] | None = None,
    ) -> None:
        self.id = 7
        self.user_id = 77
        self.order_id = 'tp123_abcdef12'
        self.tabpay_payment_id = None
        self.amount_kopeks = amount_kopeks
        self.commission_kopeks = None
        self.status = status
        self.is_paid = is_paid
        self.is_test = False
        self.paid_at = None
        self.updated_at = None
        self.callback_payload = None
        self.metadata_json = {}
        self.processed_events = processed_events if processed_events is not None else []
        self.transaction_id = None


class StubTabPayService:
    """Заглушка API-клиента: возвращает заготовленный объект платежа."""

    API_MIN_AMOUNT_KOPEKS = TabPayService.API_MIN_AMOUNT_KOPEKS
    API_MAX_AMOUNT_KOPEKS = TabPayService.API_MAX_AMOUNT_KOPEKS

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response or {
            'id': '6b9d2c88-4b1a-4f0e-9c37-1f2ab34cd561',
            'orderId': 'echo',
            'status': 'CREATED',
            'amountKopecks': 50000,
            'commissionKopecks': 3500,
            'payUrl': 'https://tabpay.org/pay/6b9d2c88-4b1a-4f0e-9c37-1f2ab34cd561',
            'isTest': False,
        }
        self.calls: list[dict[str, Any]] = []

    async def create_payment_reconciled(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


def _make_service() -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    return service


def _enable_tabpay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'TABPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'TABPAY_API_KEY', 'tp_test_key', raising=False)
    monkeypatch.setattr(settings, 'TABPAY_WEBHOOK_SECRET', 'whsec_test', raising=False)
    monkeypatch.setattr(settings, 'TABPAY_MIN_AMOUNT_KOPEKS', 10000, raising=False)
    monkeypatch.setattr(settings, 'TABPAY_MAX_AMOUNT_KOPEKS', 10000000, raising=False)
    monkeypatch.setattr(settings, 'TABPAY_CARD_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'TABPAY_SBP_ENABLED', False, raising=False)


def _patch_user_lookup(monkeypatch: pytest.MonkeyPatch, telegram_id: int | None = 123456) -> None:
    async def fake_get_user_by_id(_db: Any, _user_id: int) -> Any:
        if telegram_id is None:
            return None

        class _User:
            pass

        user = _User()
        user.telegram_id = telegram_id
        return user

    monkeypatch.setattr(payment_service_module, 'get_user_by_id', fake_get_user_by_id, raising=False)


# ---------------------------------------------------------------------------
# create_tabpay_payment
# ---------------------------------------------------------------------------


@pytest.mark.anyio('asyncio')
async def test_create_tabpay_payment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_tabpay(monkeypatch)
    stub = StubTabPayService()
    monkeypatch.setattr(tabpay_mixin_module, 'tabpay_service', stub)
    _patch_user_lookup(monkeypatch)

    captured: dict[str, Any] = {}

    async def fake_create_tabpay_payment(**kwargs: Any) -> DummyLocalPayment:
        captured.update(kwargs)
        return DummyLocalPayment(payment_id=999)

    monkeypatch.setattr(tabpay_crud_module, 'create_tabpay_payment', fake_create_tabpay_payment)

    service = _make_service()
    result = await service.create_tabpay_payment(
        db=DummySession(),
        user_id=77,
        amount_kopeks=50000,
        description='Пополнение баланса',
    )

    assert result is not None
    assert result['local_payment_id'] == 999
    assert result['payment_url'] == 'https://tabpay.org/pay/6b9d2c88-4b1a-4f0e-9c37-1f2ab34cd561'
    assert result['amount_kopeks'] == 50000
    assert result['order_id'].startswith('tp123456_')

    api_call = stub.calls[0]
    assert api_call['amount_kopecks'] == 50000
    # Оба способа доступны магазину — навязывать method нельзя, выбирает покупатель
    assert api_call['method'] is None
    assert api_call['telegram_id'] == 123456
    assert api_call['order_id'] == result['order_id']
    assert captured['commission_kopeks'] == 3500
    assert captured['is_test'] is False


@pytest.mark.anyio('asyncio')
async def test_create_tabpay_payment_sbp_sub_method(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_tabpay(monkeypatch)
    stub = StubTabPayService()
    monkeypatch.setattr(tabpay_mixin_module, 'tabpay_service', stub)
    _patch_user_lookup(monkeypatch, telegram_id=None)

    async def fake_create_tabpay_payment(**_kwargs: Any) -> DummyLocalPayment:
        return DummyLocalPayment()

    monkeypatch.setattr(tabpay_crud_module, 'create_tabpay_payment', fake_create_tabpay_payment)

    service = _make_service()
    result = await service.create_tabpay_payment(
        db=DummySession(),
        user_id=77,
        amount_kopeks=50000,
        payment_method_type='sbp',
    )

    assert result is not None
    assert stub.calls[0]['method'] == 'SBP'


@pytest.mark.anyio('asyncio')
async def test_generic_method_pins_the_only_enabled_option(monkeypatch: pytest.MonkeyPatch) -> None:
    """SBP-only магазин: генерик-метод обязан слать SBP, иначе TabPay ответит 409."""
    _enable_tabpay(monkeypatch)
    monkeypatch.setattr(settings, 'TABPAY_SBP_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'TABPAY_CARD_ENABLED', False, raising=False)

    stub = StubTabPayService()
    monkeypatch.setattr(tabpay_mixin_module, 'tabpay_service', stub)
    _patch_user_lookup(monkeypatch, telegram_id=None)

    async def fake_create_tabpay_payment(**_kwargs: Any) -> DummyLocalPayment:
        return DummyLocalPayment()

    monkeypatch.setattr(tabpay_crud_module, 'create_tabpay_payment', fake_create_tabpay_payment)

    service = _make_service()
    await service.create_tabpay_payment(db=DummySession(), user_id=77, amount_kopeks=50000)

    assert stub.calls[0]['method'] == 'SBP'


@pytest.mark.anyio('asyncio')
async def test_create_tabpay_payment_respects_amount_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_tabpay(monkeypatch)
    monkeypatch.setattr(tabpay_mixin_module, 'tabpay_service', StubTabPayService())

    service = _make_service()
    result_low = await service.create_tabpay_payment(db=DummySession(), user_id=77, amount_kopeks=9999)
    result_high = await service.create_tabpay_payment(db=DummySession(), user_id=77, amount_kopeks=10000001)

    assert result_low is None
    assert result_high is None


@pytest.mark.anyio('asyncio')
async def test_create_tabpay_payment_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'TABPAY_ENABLED', False, raising=False)

    service = _make_service()
    result = await service.create_tabpay_payment(db=DummySession(), user_id=77, amount_kopeks=50000)

    assert result is None


# ---------------------------------------------------------------------------
# Сетевые сбои при создании: сверка по своему orderId
# ---------------------------------------------------------------------------


class RecordingTabPayService(TabPayService):
    """Клиент с подменёнными сетевыми вызовами: считает попытки создания."""

    def __init__(self, create_results: list[Any], lookup_results: list[Any]) -> None:
        super().__init__()
        self.create_results = list(create_results)
        self.lookup_results = list(lookup_results)
        self.create_calls: list[str] = []
        self.lookup_calls: list[str] = []

    async def create_payment(self, *, order_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.create_calls.append(order_id)
        outcome = self.create_results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def get_payment_by_order_id(self, order_id: str) -> dict[str, Any] | None:
        self.lookup_calls.append(order_id)
        return self.lookup_results.pop(0)


def _api_payment(order_id: str = 'tp1_x') -> dict[str, Any]:
    return {
        'id': '6b9d2c88-4b1a-4f0e-9c37-1f2ab34cd561',
        'orderId': order_id,
        'status': 'CREATED',
        'amountKopecks': 50000,
        'payUrl': 'https://tabpay.org/pay/6b9d2c88-4b1a-4f0e-9c37-1f2ab34cd561',
    }


@pytest.mark.anyio('asyncio')
async def test_network_failure_reuses_existing_payment() -> None:
    """Ответ потерян, но платёж создался: берём его payUrl, а не плодим второй."""
    existing = _api_payment()
    client = RecordingTabPayService(
        create_results=[TabPayNetworkError('connection reset')],
        lookup_results=[existing],
    )

    result = await client.create_payment_reconciled(order_id='tp1_x', amount_kopecks=50000)

    assert result == existing
    assert client.create_calls == ['tp1_x']  # повтора создания не было
    assert client.lookup_calls == ['tp1_x']


@pytest.mark.anyio('asyncio')
async def test_network_failure_recreates_when_nothing_found() -> None:
    """Платежа нет — только тогда создаём заново, с тем же orderId."""
    created = _api_payment()
    client = RecordingTabPayService(
        create_results=[TabPayNetworkError('timeout'), created],
        lookup_results=[None],
    )

    result = await client.create_payment_reconciled(order_id='tp1_x', amount_kopecks=50000)

    assert result == created
    assert client.create_calls == ['tp1_x', 'tp1_x']


@pytest.mark.anyio('asyncio')
async def test_duplicate_order_id_returns_existing_payment() -> None:
    """409 на дубль orderId — платёж уже есть, повторять создание нечем."""
    existing = _api_payment()
    client = RecordingTabPayService(
        create_results=[TabPayAPIError(409, 'orderId already used')],
        lookup_results=[existing],
    )

    result = await client.create_payment_reconciled(order_id='tp1_x', amount_kopecks=50000)

    assert result == existing
    assert client.create_calls == ['tp1_x']


@pytest.mark.anyio('asyncio')
async def test_validation_error_is_not_retried() -> None:
    """400 — наша ошибка в запросе: ни сверки, ни повтора."""
    client = RecordingTabPayService(
        create_results=[TabPayAPIError(400, 'amountKopecks must not be less than 100')],
        lookup_results=[],
    )

    with pytest.raises(TabPayAPIError):
        await client.create_payment_reconciled(order_id='tp1_x', amount_kopecks=1)

    assert client.lookup_calls == []


@pytest.mark.anyio('asyncio')
async def test_server_error_reconciles_before_recreating() -> None:
    """5xx — исход неизвестен, действуем как при сетевом сбое."""
    existing = _api_payment()
    client = RecordingTabPayService(
        create_results=[TabPayAPIError(502, 'bad gateway')],
        lookup_results=[existing],
    )

    result = await client.create_payment_reconciled(order_id='tp1_x', amount_kopecks=50000)

    assert result == existing
    assert client.create_calls == ['tp1_x']


# ---------------------------------------------------------------------------
# process_tabpay_callback
# ---------------------------------------------------------------------------


def _patch_callback_crud(monkeypatch: pytest.MonkeyPatch, payment: FakeTabPayPayment | None) -> AsyncMock:
    async def fake_get_by_order_id(_db: Any, _order_id: str) -> FakeTabPayPayment | None:
        return payment

    async def fake_get_for_update(_db: Any, _payment_id: int) -> FakeTabPayPayment | None:
        return payment

    update_mock = AsyncMock(return_value=payment)
    monkeypatch.setattr(tabpay_crud_module, 'get_tabpay_payment_by_order_id', fake_get_by_order_id)
    monkeypatch.setattr(tabpay_crud_module, 'get_tabpay_payment_by_id_for_update', fake_get_for_update)
    monkeypatch.setattr(tabpay_crud_module, 'update_tabpay_payment_status', update_mock)
    return update_mock


def _webhook_payload(status: str = 'SUCCESS', amount: int | None = 50000) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'id': '6b9d2c88-4b1a-4f0e-9c37-1f2ab34cd561',
        'orderId': 'tp123_abcdef12',
        'status': status,
        'telegramId': '123456',
        'metadata': {'user_id': 77},
        'test': False,
    }
    if amount is not None:
        payload['amountKopecks'] = amount
    return payload


@pytest.mark.anyio('asyncio')
async def test_callback_success_finalizes(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakeTabPayPayment()
    _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_tabpay_payment', finalize_mock, raising=False)

    result = await service.process_tabpay_callback(DummySession(), _webhook_payload())

    assert result is True
    assert payment.is_paid is True
    assert payment.status == 'success'
    assert payment.tabpay_payment_id == '6b9d2c88-4b1a-4f0e-9c37-1f2ab34cd561'
    assert payment.processed_events == ['6b9d2c88-4b1a-4f0e-9c37-1f2ab34cd561:SUCCESS']
    finalize_mock.assert_awaited_once()


@pytest.mark.anyio('asyncio')
async def test_callback_is_idempotent_by_id_and_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Повторная доставка того же события не зачисляет баланс второй раз."""
    payment = FakeTabPayPayment(
        status='success',
        is_paid=True,
        processed_events=['6b9d2c88-4b1a-4f0e-9c37-1f2ab34cd561:SUCCESS'],
    )
    update_mock = _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_tabpay_payment', finalize_mock, raising=False)

    result = await service.process_tabpay_callback(DummySession(), _webhook_payload())

    assert result is True
    finalize_mock.assert_not_awaited()
    update_mock.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_late_payment_after_expired_is_credited(monkeypatch: pytest.MonkeyPatch) -> None:
    """QR СБП оплатили после таймаута: EXPIRED -> SUCCESS обязан зачислиться."""
    payment = FakeTabPayPayment(
        status='expired',
        processed_events=['6b9d2c88-4b1a-4f0e-9c37-1f2ab34cd561:EXPIRED'],
    )
    _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_tabpay_payment', finalize_mock, raising=False)

    result = await service.process_tabpay_callback(DummySession(), _webhook_payload())

    assert result is True
    assert payment.is_paid is True
    finalize_mock.assert_awaited_once()


@pytest.mark.anyio('asyncio')
async def test_callback_amount_mismatch_does_not_credit(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakeTabPayPayment(amount_kopeks=50000)
    update_mock = _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_tabpay_payment', finalize_mock, raising=False)

    result = await service.process_tabpay_callback(DummySession(), _webhook_payload(amount=49999))

    assert result is False
    finalize_mock.assert_not_awaited()
    assert update_mock.await_args.kwargs['status'] == 'amount_mismatch'
    assert update_mock.await_args.kwargs['is_paid'] is False


@pytest.mark.anyio('asyncio')
async def test_callback_missing_amount_does_not_credit(monkeypatch: pytest.MonkeyPatch) -> None:
    """SUCCESS без amountKopecks: сверять нечего — оставляем платёж под ретрай."""
    payment = FakeTabPayPayment()
    update_mock = _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_tabpay_payment', finalize_mock, raising=False)

    result = await service.process_tabpay_callback(DummySession(), _webhook_payload(amount=None))

    assert result is False
    finalize_mock.assert_not_awaited()
    update_mock.assert_not_awaited()
    assert payment.is_paid is False
    assert payment.status == 'pending'
    assert payment.processed_events == []  # событие не помечено — повтор ещё сработает


@pytest.mark.anyio('asyncio')
async def test_callback_test_button_webhook_touches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Кнопка «Отправить тестовый вебхук»: id вида test-..., orderId не наш."""
    update_mock = _patch_callback_crud(monkeypatch, None)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_tabpay_payment', finalize_mock, raising=False)

    payload = _webhook_payload()
    payload['test'] = True
    payload['id'] = 'test-0e2c1f'
    payload['orderId'] = 'order-1001'
    result = await service.process_tabpay_callback(DummySession(), payload)

    assert result is True  # доставку подтверждаем
    finalize_mock.assert_not_awaited()
    update_mock.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_callback_non_final_status_updates_record(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakeTabPayPayment()
    update_mock = _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_tabpay_payment', finalize_mock, raising=False)

    result = await service.process_tabpay_callback(DummySession(), _webhook_payload(status='FAILED'))

    assert result is True
    finalize_mock.assert_not_awaited()
    assert update_mock.await_args.kwargs['status'] == 'declined'


@pytest.mark.anyio('asyncio')
async def test_callback_refund_after_success_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakeTabPayPayment(status='success', is_paid=True)
    update_mock = _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    result = await service.process_tabpay_callback(DummySession(), _webhook_payload(status='REFUNDED'))

    assert result is True
    assert update_mock.await_args.kwargs['status'] == 'refunded'


@pytest.mark.anyio('asyncio')
async def test_callback_ignores_events_after_final_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Из CANCELED/REFUNDED переходов нет — платёж не «чинится» новым вебхуком."""
    payment = FakeTabPayPayment(status='canceled')
    update_mock = _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_tabpay_payment', finalize_mock, raising=False)

    result = await service.process_tabpay_callback(DummySession(), _webhook_payload())

    assert result is True
    finalize_mock.assert_not_awaited()
    update_mock.assert_not_awaited()
    assert payment.is_paid is False


@pytest.mark.anyio('asyncio')
async def test_callback_unknown_status_is_acknowledged(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakeTabPayPayment()
    update_mock = _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    result = await service.process_tabpay_callback(DummySession(), _webhook_payload(status='CHARGEBACK'))

    assert result is True
    update_mock.assert_not_awaited()
    assert payment.is_paid is False


@pytest.mark.anyio('asyncio')
async def test_callback_missing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    assert await service.process_tabpay_callback(DummySession(), {'id': 'x'}) is False


@pytest.mark.anyio('asyncio')
async def test_callback_unknown_order_is_acknowledged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Чужой orderId повторами не появится — подтверждаем доставку."""
    _patch_callback_crud(monkeypatch, None)

    service = _make_service()
    assert await service.process_tabpay_callback(DummySession(), _webhook_payload()) is True


# ---------------------------------------------------------------------------
# verify_webhook_signature (схема v2)
# ---------------------------------------------------------------------------


def _sign(secret: str, timestamp: str, raw_body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b'.' + raw_body, hashlib.sha256).hexdigest()


RAW_BODY = b'{"id":"6b9d","orderId":"tp1_x","status":"SUCCESS","amountKopecks":50000}'


def test_verify_webhook_signature_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'TABPAY_WEBHOOK_SECRET', 'whsec_test', raising=False)
    monkeypatch.setattr(settings, 'TABPAY_WEBHOOK_MAX_AGE_SECONDS', 300, raising=False)
    service = TabPayService()

    ts = str(int(time.time()))
    assert service.verify_webhook_signature(RAW_BODY, ts, _sign('whsec_test', ts, RAW_BODY)) is True
    # Регистр hex-подписи не важен
    assert service.verify_webhook_signature(RAW_BODY, ts, _sign('whsec_test', ts, RAW_BODY).upper()) is True


def test_verify_webhook_signature_rejects_stale_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Переигрывание перехваченного вебхука отсекается окном свежести."""
    monkeypatch.setattr(settings, 'TABPAY_WEBHOOK_SECRET', 'whsec_test', raising=False)
    monkeypatch.setattr(settings, 'TABPAY_WEBHOOK_MAX_AGE_SECONDS', 300, raising=False)
    service = TabPayService()

    stale = str(int(time.time()) - 301)
    assert service.verify_webhook_signature(RAW_BODY, stale, _sign('whsec_test', stale, RAW_BODY)) is False

    future = str(int(time.time()) + 301)
    assert service.verify_webhook_signature(RAW_BODY, future, _sign('whsec_test', future, RAW_BODY)) is False


def test_verify_webhook_signature_timestamp_is_part_of_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подпись считается от «метка.тело», а не от одного тела."""
    monkeypatch.setattr(settings, 'TABPAY_WEBHOOK_SECRET', 'whsec_test', raising=False)
    service = TabPayService()

    ts = str(int(time.time()))
    body_only = hmac.new(b'whsec_test', RAW_BODY, hashlib.sha256).hexdigest()

    assert service.verify_webhook_signature(RAW_BODY, ts, body_only) is False


def test_verify_webhook_signature_rejects_tampered_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'TABPAY_WEBHOOK_SECRET', 'whsec_test', raising=False)
    service = TabPayService()

    ts = str(int(time.time()))
    signature = _sign('whsec_test', ts, RAW_BODY)
    tampered = RAW_BODY.replace(b'50000', b'99999')

    assert service.verify_webhook_signature(tampered, ts, signature) is False


def test_verify_webhook_signature_rejects_wrong_key_and_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'TABPAY_WEBHOOK_SECRET', 'whsec_test', raising=False)
    service = TabPayService()

    ts = str(int(time.time()))
    assert service.verify_webhook_signature(RAW_BODY, ts, _sign('other', ts, RAW_BODY)) is False
    assert service.verify_webhook_signature(RAW_BODY, ts, None) is False
    assert service.verify_webhook_signature(RAW_BODY, ts, '') is False
    assert service.verify_webhook_signature(RAW_BODY, None, _sign('whsec_test', ts, RAW_BODY)) is False
    assert service.verify_webhook_signature(RAW_BODY, 'not-a-number', _sign('whsec_test', ts, RAW_BODY)) is False


def test_verify_webhook_signature_blank_secret_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустой секрет — подпись подделал бы кто угодно, поэтому отказ."""
    monkeypatch.setattr(settings, 'TABPAY_WEBHOOK_SECRET', '', raising=False)
    service = TabPayService()

    ts = str(int(time.time()))
    forged = _sign('', ts, RAW_BODY)

    assert service.verify_webhook_signature(RAW_BODY, ts, forged) is False


def test_is_tabpay_enabled_requires_both_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'TABPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'TABPAY_API_KEY', 'tp_key', raising=False)
    monkeypatch.setattr(settings, 'TABPAY_WEBHOOK_SECRET', '', raising=False)

    assert settings.is_tabpay_enabled() is False

    monkeypatch.setattr(settings, 'TABPAY_WEBHOOK_SECRET', 'whsec_test', raising=False)
    assert settings.is_tabpay_enabled() is True


# ---------------------------------------------------------------------------
# Тестовые платежи (песочница) не должны зачислять баланс НИ ОДНИМ путём
# ---------------------------------------------------------------------------


@pytest.mark.anyio('asyncio')
async def test_api_check_does_not_credit_test_payment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Магазин-песочница: деньги не двигались, зачислять нечего.

    Вебхук такой платёж отсекает по полю test, но сверка по API шла мимо этой
    проверки — и SUCCESS из песочницы зачислялся на реальный баланс.
    """
    payment = FakeTabPayPayment()
    payment.is_test = True
    payment.tabpay_payment_id = '6b9d2c88-4b1a-4f0e-9c37-1f2ab34cd561'
    _patch_callback_crud(monkeypatch, payment)

    class _Api:
        async def get_payment(self, _payment_id: str) -> dict[str, Any]:
            return {
                'id': '6b9d2c88-4b1a-4f0e-9c37-1f2ab34cd561',
                'status': 'SUCCESS',
                'amountKopecks': 50000,
                'isTest': True,
            }

    monkeypatch.setattr(tabpay_mixin_module, 'tabpay_service', _Api())

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_tabpay_payment', finalize_mock, raising=False)

    result = await service.check_tabpay_payment_status(DummySession(), payment.order_id)

    assert result is not None
    assert result['is_paid'] is False
    assert payment.is_paid is False
    finalize_mock.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_finalize_refuses_test_payment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Единая точка зачисления обязана сама отсекать тестовые платежи."""
    payment = FakeTabPayPayment(status='success', is_paid=True)
    payment.is_test = True

    called: list[str] = []

    async def fail_get_user(*_args: Any, **_kwargs: Any) -> Any:
        called.append('get_user_by_id')
        raise AssertionError('зачисление не должно начинаться для тестового платежа')

    monkeypatch.setattr(payment_service_module, 'get_user_by_id', fail_get_user, raising=False)

    service = _make_service()
    result = await service._finalize_tabpay_payment(DummySession(), payment, trigger='webhook')

    assert result is True
    assert called == []
    assert payment.transaction_id is None


@pytest.mark.anyio('asyncio')
async def test_callback_records_sandbox_payment_without_crediting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Платёж песочницы — наш, его исход фиксируем, но баланс не трогаем."""
    payment = FakeTabPayPayment()
    update_mock = _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_tabpay_payment', finalize_mock, raising=False)

    payload = _webhook_payload()
    payload['test'] = True
    result = await service.process_tabpay_callback(DummySession(), payload)

    assert result is True
    finalize_mock.assert_not_awaited()
    assert payment.is_paid is False
    assert payment.is_test is True
    assert update_mock.await_args.kwargs['status'] == 'success'
    assert update_mock.await_args.kwargs['is_paid'] is None


@pytest.mark.anyio('asyncio')
async def test_callback_treats_stringy_test_flag_as_test(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нестрогое значение флага толкуем в сторону «не зачислять»."""
    payment = FakeTabPayPayment()
    _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_tabpay_payment', finalize_mock, raising=False)

    payload = _webhook_payload()
    payload['test'] = 'true'
    result = await service.process_tabpay_callback(DummySession(), payload)

    assert result is True
    finalize_mock.assert_not_awaited()
    assert payment.is_paid is False


# ---------------------------------------------------------------------------
# Защита от тестовых платежей НЕ ДОЛЖНА ломать боевое флоу
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'raw_is_test',
    [None, False, 0, 'false', 'False', '0', '', 'no', 'что-то невнятное', 42],
    ids=['нет поля', 'false', '0', '"false"', '"False"', '"0"', 'пусто', '"no"', 'мусор', 'число'],
)
@pytest.mark.anyio('asyncio')
async def test_create_marks_test_only_on_explicit_yes(monkeypatch: pytest.MonkeyPatch, raw_is_test: Any) -> None:
    """Боевой платёж не должен помечаться тестовым из-за нестрогого isTest.

    Помеченный так платёж уже никогда не зачислится: защита стоит в единственной
    точке зачисления. Тестовым считаем ТОЛЬКО явное «да».
    """
    _enable_tabpay(monkeypatch)
    response = dict(StubTabPayService().response)
    if raw_is_test is None:
        response.pop('isTest', None)
    else:
        response['isTest'] = raw_is_test
    stub = StubTabPayService(response)
    monkeypatch.setattr(tabpay_mixin_module, 'tabpay_service', stub)
    _patch_user_lookup(monkeypatch)

    captured: dict[str, Any] = {}

    async def fake_create(**kwargs: Any) -> DummyLocalPayment:
        captured.update(kwargs)
        return DummyLocalPayment()

    monkeypatch.setattr(tabpay_crud_module, 'create_tabpay_payment', fake_create)

    service = _make_service()
    await service.create_tabpay_payment(db=DummySession(), user_id=77, amount_kopeks=50000)

    assert captured['is_test'] is False


@pytest.mark.parametrize('raw_is_test', [True, 'true', 'True', '1', 'yes'])
@pytest.mark.anyio('asyncio')
async def test_create_marks_test_on_explicit_yes(monkeypatch: pytest.MonkeyPatch, raw_is_test: Any) -> None:
    _enable_tabpay(monkeypatch)
    response = dict(StubTabPayService().response)
    response['isTest'] = raw_is_test
    monkeypatch.setattr(tabpay_mixin_module, 'tabpay_service', StubTabPayService(response))
    _patch_user_lookup(monkeypatch)

    captured: dict[str, Any] = {}

    async def fake_create(**kwargs: Any) -> DummyLocalPayment:
        captured.update(kwargs)
        return DummyLocalPayment()

    monkeypatch.setattr(tabpay_crud_module, 'create_tabpay_payment', fake_create)

    service = _make_service()
    await service.create_tabpay_payment(db=DummySession(), user_id=77, amount_kopeks=50000)

    assert captured['is_test'] is True


@pytest.mark.parametrize(
    'raw_test',
    [False, 0, 'false', 'False', '0', '', 'no', 'что-то невнятное', 42, None],
    ids=['false', '0', '"false"', '"False"', '"0"', 'пусто', '"no"', 'мусор', 'число', 'нет поля'],
)
@pytest.mark.anyio('asyncio')
async def test_webhook_credits_unless_test_is_explicit_yes(monkeypatch: pytest.MonkeyPatch, raw_test: Any) -> None:
    """Боевой вебхук обязан зачислять: нераспознанный флаг не повод съесть оплату."""
    payment = FakeTabPayPayment()
    _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_tabpay_payment', finalize_mock, raising=False)

    payload = _webhook_payload()
    if raw_test is None:
        del payload['test']
    else:
        payload['test'] = raw_test

    result = await service.process_tabpay_callback(DummySession(), payload)

    assert result is True
    assert payment.is_paid is True
    assert payment.is_test is False
    finalize_mock.assert_awaited_once()


@pytest.mark.anyio('asyncio')
async def test_api_check_credits_production_payment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сверка по API тоже обязана зачислять боевой платёж."""
    payment = FakeTabPayPayment()
    payment.tabpay_payment_id = '6b9d2c88-4b1a-4f0e-9c37-1f2ab34cd561'
    _patch_callback_crud(monkeypatch, payment)

    class _Api:
        async def get_payment(self, _payment_id: str) -> dict[str, Any]:
            return {
                'id': '6b9d2c88-4b1a-4f0e-9c37-1f2ab34cd561',
                'status': 'SUCCESS',
                'amountKopecks': 50000,
                'isTest': False,
            }

    monkeypatch.setattr(tabpay_mixin_module, 'tabpay_service', _Api())

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_tabpay_payment', finalize_mock, raising=False)

    await service.check_tabpay_payment_status(DummySession(), payment.order_id)

    assert payment.is_paid is True
    finalize_mock.assert_awaited_once()


@pytest.mark.anyio('asyncio')
async def test_finalize_credits_production_payment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Страж в точке зачисления не должен срабатывать на боевом платеже."""
    payment = FakeTabPayPayment(status='success', is_paid=True)
    assert payment.is_test is False

    reached: list[str] = []

    async def fake_get_user(_db: Any, _user_id: int) -> None:
        # Дальше зачисление всё равно прервётся — нам важен сам факт прохода стража
        reached.append('get_user_by_id')

    monkeypatch.setattr(payment_service_module, 'get_user_by_id', fake_get_user, raising=False)

    service = _make_service()
    await service._finalize_tabpay_payment(DummySession(), payment, trigger='webhook')

    assert reached == ['get_user_by_id'], 'боевой платёж не дошёл до зачисления'
