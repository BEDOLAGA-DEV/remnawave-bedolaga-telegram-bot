"""Сценарии ParityPay в PaymentService.

Покрывают создание счёта (рубли на входе API против копеек у нас, sub-методы,
лимиты, восстановление после сетевого сбоя и занятого order_id), обработку
HTTP-уведомления (зачисление, сверка суммы, идемпотентность по паре (id, status),
поздняя оплата) и маршрутизацию уведомлений о подписках, которые приходят на
тот же адрес.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.database.crud.paritypay as paritypay_crud_module
import app.services.payment.paritypay as paritypay_mixin_module
from app.config import settings
from app.services import payment_service as payment_service_module
from app.services.paritypay_service import ParityPayAPIError, ParityPayNetworkError, ParityPayService
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
    def __init__(self, payment_id: int = 601) -> None:
        self.id = payment_id
        self.created_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class FakeParityPayPayment:
    def __init__(
        self,
        *,
        status: str = 'pending',
        is_paid: bool = False,
        amount_kopeks: int = 125000,
        processed_events: list[str] | None = None,
    ) -> None:
        self.id = 11
        self.user_id = 77
        self.order_id = 'pp123_abcdef12'
        self.paritypay_payment_id = None
        self.amount_kopeks = amount_kopeks
        self.credited_kopeks = None
        self.status = status
        self.is_paid = is_paid
        self.paid_at = None
        self.updated_at = None
        self.callback_payload = None
        self.metadata_json = {}
        self.processed_events = processed_events if processed_events is not None else []
        self.transaction_id = None


class StubParityPayService:
    """Заглушка клиента: возвращает заготовленный счёт."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response or {
            'id': '9beea835-0937-4b5c-8f5a-c3a0d0e60346',
            'order_id': 'echo',
            'amount': 1250.0,
            'service': None,
            'status': 'NEW',
            'link': 'https://pay.paritypay.net/9beea835-0937-4b5c-8f5a-c3a0d0e60346',
        }
        self.calls: list[dict[str, Any]] = []

    async def create_invoice_reconciled(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


def _make_service() -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    return service


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'PARITYPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'PARITYPAY_SHOP_ID', '874dfb1e-shop', raising=False)
    monkeypatch.setattr(settings, 'PARITYPAY_SECRET_KEY', 'secret-1', raising=False)
    monkeypatch.setattr(settings, 'PARITYPAY_CALLBACK_SECRET', 'secret-2', raising=False)
    monkeypatch.setattr(settings, 'PARITYPAY_MIN_AMOUNT_KOPEKS', 10000, raising=False)
    monkeypatch.setattr(settings, 'PARITYPAY_MAX_AMOUNT_KOPEKS', 10000000, raising=False)
    monkeypatch.setattr(settings, 'PARITYPAY_CARD_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'PARITYPAY_SBP_ENABLED', False, raising=False)


def _patch_user(monkeypatch: pytest.MonkeyPatch, telegram_id: int | None = 123456) -> None:
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
# create_paritypay_payment
# ---------------------------------------------------------------------------


@pytest.mark.anyio('asyncio')
async def test_create_sends_rubles_not_kopeks(monkeypatch: pytest.MonkeyPatch) -> None:
    """API принимает рубли: 125000 копеек должны уйти как 1250.0, а не 125000."""
    _enable(monkeypatch)
    stub = StubParityPayService()
    monkeypatch.setattr(paritypay_mixin_module, 'paritypay_service', stub)
    _patch_user(monkeypatch)

    captured: dict[str, Any] = {}

    async def fake_create(**kwargs: Any) -> DummyLocalPayment:
        captured.update(kwargs)
        return DummyLocalPayment(payment_id=999)

    monkeypatch.setattr(paritypay_crud_module, 'create_paritypay_payment', fake_create)

    service = _make_service()
    result = await service.create_paritypay_payment(
        db=DummySession(), user_id=77, amount_kopeks=125000, description='Пополнение баланса'
    )

    assert result is not None
    assert result['local_payment_id'] == 999
    assert result['payment_url'] == 'https://pay.paritypay.net/9beea835-0937-4b5c-8f5a-c3a0d0e60346'
    assert result['order_id'].startswith('pp123456_')

    call = stub.calls[0]
    assert call['amount_kopeks'] == 125000  # в клиент отдаём копейки
    assert call['service'] is None  # оба способа доступны — выбирает плательщик
    assert call['order_id'] == result['order_id']
    # В БД сохраняем копейки, не рубли
    assert captured['amount_kopeks'] == 125000


@pytest.mark.anyio('asyncio')
async def test_create_with_sbp_sub_method(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    stub = StubParityPayService()
    monkeypatch.setattr(paritypay_mixin_module, 'paritypay_service', stub)
    _patch_user(monkeypatch, telegram_id=None)
    monkeypatch.setattr(paritypay_crud_module, 'create_paritypay_payment', AsyncMock(return_value=DummyLocalPayment()))

    service = _make_service()
    await service.create_paritypay_payment(
        db=DummySession(), user_id=77, amount_kopeks=125000, payment_method_type='sbp'
    )

    assert stub.calls[0]['service'] == 'sbp'  # строчными, как требует API


@pytest.mark.anyio('asyncio')
async def test_generic_method_pins_the_only_enabled_option(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(settings, 'PARITYPAY_SBP_ENABLED', True, raising=False)
    stub = StubParityPayService()
    monkeypatch.setattr(paritypay_mixin_module, 'paritypay_service', stub)
    _patch_user(monkeypatch, telegram_id=None)
    monkeypatch.setattr(paritypay_crud_module, 'create_paritypay_payment', AsyncMock(return_value=DummyLocalPayment()))

    service = _make_service()
    await service.create_paritypay_payment(db=DummySession(), user_id=77, amount_kopeks=125000)

    assert stub.calls[0]['service'] == 'sbp'


@pytest.mark.anyio('asyncio')
async def test_create_respects_limits_and_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(paritypay_mixin_module, 'paritypay_service', StubParityPayService())
    service = _make_service()

    assert await service.create_paritypay_payment(db=DummySession(), user_id=77, amount_kopeks=9999) is None
    assert await service.create_paritypay_payment(db=DummySession(), user_id=77, amount_kopeks=10000001) is None

    monkeypatch.setattr(settings, 'PARITYPAY_ENABLED', False, raising=False)
    assert await service.create_paritypay_payment(db=DummySession(), user_id=77, amount_kopeks=125000) is None


# ---------------------------------------------------------------------------
# Сетевые сбои и занятый order_id
# ---------------------------------------------------------------------------


class RecordingClient(ParityPayService):
    def __init__(self, create_results: list[Any], lookup_results: list[Any]) -> None:
        super().__init__()
        self.create_results = list(create_results)
        self.lookup_results = list(lookup_results)
        self.create_calls: list[str] = []
        self.lookup_calls: list[str] = []

    async def create_invoice(self, *, order_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.create_calls.append(order_id)
        outcome = self.create_results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def get_invoice(self, *, invoice_id: str | None = None, order_id: str | None = None) -> dict[str, Any] | None:
        self.lookup_calls.append(order_id or invoice_id or '')
        return self.lookup_results.pop(0)


def _invoice() -> dict[str, Any]:
    return {
        'id': '9beea835-0937-4b5c-8f5a-c3a0d0e60346',
        'order_id': 'pp1_x',
        'amount': 1250.0,
        'status': 'NEW',
        'link': 'https://pay.paritypay.net/9beea835',
    }


@pytest.mark.anyio('asyncio')
async def test_network_failure_reuses_existing_invoice() -> None:
    existing = _invoice()
    client = RecordingClient([ParityPayNetworkError('reset')], [existing])

    assert await client.create_invoice_reconciled(order_id='pp1_x', amount_kopeks=125000) == existing
    assert client.create_calls == ['pp1_x']  # повтора не было
    assert client.lookup_calls == ['pp1_x']


@pytest.mark.anyio('asyncio')
async def test_network_failure_recreates_when_nothing_found() -> None:
    created = _invoice()
    client = RecordingClient([ParityPayNetworkError('timeout'), created], [None])

    assert await client.create_invoice_reconciled(order_id='pp1_x', amount_kopeks=125000) == created
    assert client.create_calls == ['pp1_x', 'pp1_x']


@pytest.mark.anyio('asyncio')
async def test_duplicate_order_id_422_returns_existing() -> None:
    """«Order id is not unique» — счёт уже есть, создавать нечего."""
    existing = _invoice()
    client = RecordingClient([ParityPayAPIError(422, 'Order id is not unique')], [existing])

    assert await client.create_invoice_reconciled(order_id='pp1_x', amount_kopeks=125000) == existing
    assert client.create_calls == ['pp1_x']


@pytest.mark.anyio('asyncio')
async def test_other_422_is_not_retried() -> None:
    """Недопустимый service — повторами не лечится."""
    client = RecordingClient([ParityPayAPIError(422, "Service 'foo' is not a valid.")], [None])

    with pytest.raises(ParityPayAPIError):
        await client.create_invoice_reconciled(order_id='pp1_x', amount_kopeks=125000)


@pytest.mark.anyio('asyncio')
async def test_validation_400_is_not_retried() -> None:
    client = RecordingClient([ParityPayAPIError(400, 'bad request')], [])

    with pytest.raises(ParityPayAPIError):
        await client.create_invoice_reconciled(order_id='pp1_x', amount_kopeks=125000)
    assert client.lookup_calls == []


# ---------------------------------------------------------------------------
# process_paritypay_callback
# ---------------------------------------------------------------------------


def _patch_crud(monkeypatch: pytest.MonkeyPatch, payment: FakeParityPayPayment | None) -> AsyncMock:
    async def fake_by_order(_db: Any, _order_id: str) -> FakeParityPayPayment | None:
        return payment

    async def fake_for_update(_db: Any, _payment_id: int) -> FakeParityPayPayment | None:
        return payment

    update_mock = AsyncMock(return_value=payment)
    monkeypatch.setattr(paritypay_crud_module, 'get_paritypay_payment_by_order_id', fake_by_order)
    monkeypatch.setattr(paritypay_crud_module, 'get_paritypay_payment_by_id_for_update', fake_for_update)
    monkeypatch.setattr(paritypay_crud_module, 'update_paritypay_payment_status', update_mock)
    return update_mock


def _callback(status: str = 'PAID', amount: Any = '1250.00') -> dict[str, Any]:
    body: dict[str, Any] = {
        'id': '9beea835-0937-4b5c-8f5a-c3a0d0e60346',
        'order_id': 'pp123_abcdef12',
        'shop_id': '874dfb1e-dbdb-4747-a1c0-005969725b74',
        'credited': '1209.01',
        'comment': None,
        'service': 'sbp',
        'custom_fields': None,
        'expires': '2026-08-24 13:40:00',
        'created': '2026-08-24 12:40:00',
        'status': status,
    }
    if amount is not None:
        body['amount'] = amount
    return body


@pytest.mark.anyio('asyncio')
async def test_callback_paid_credits_and_stores_credited(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сумма приходит СТРОКОЙ в рублях — сверка обязана сойтись с копейками."""
    payment = FakeParityPayPayment(amount_kopeks=125000)
    _patch_crud(monkeypatch, payment)

    service = _make_service()
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_paritypay_payment', finalize, raising=False)

    assert await service.process_paritypay_callback(DummySession(), _callback()) is True
    assert payment.is_paid is True
    assert payment.status == 'success'
    assert payment.credited_kopeks == 120901  # credited, не сумма счёта
    assert payment.processed_events == ['9beea835-0937-4b5c-8f5a-c3a0d0e60346:PAID']
    finalize.assert_awaited_once()


@pytest.mark.anyio('asyncio')
async def test_callback_amount_mismatch_does_not_credit(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakeParityPayPayment(amount_kopeks=125000)
    update = _patch_crud(monkeypatch, payment)

    service = _make_service()
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_paritypay_payment', finalize, raising=False)

    assert await service.process_paritypay_callback(DummySession(), _callback(amount='1249.99')) is False
    finalize.assert_not_awaited()
    assert update.await_args.kwargs['status'] == 'amount_mismatch'


@pytest.mark.anyio('asyncio')
async def test_callback_unparseable_amount_leaves_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakeParityPayPayment()
    update = _patch_crud(monkeypatch, payment)

    service = _make_service()
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_paritypay_payment', finalize, raising=False)

    assert await service.process_paritypay_callback(DummySession(), _callback(amount=None)) is False
    finalize.assert_not_awaited()
    update.assert_not_awaited()
    assert payment.status == 'pending'
    assert payment.processed_events == []  # повтор ещё может закрыть счёт


@pytest.mark.anyio('asyncio')
async def test_callback_idempotent_by_id_and_status(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakeParityPayPayment(
        status='success', is_paid=True, processed_events=['9beea835-0937-4b5c-8f5a-c3a0d0e60346:PAID']
    )
    update = _patch_crud(monkeypatch, payment)

    service = _make_service()
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_paritypay_payment', finalize, raising=False)

    assert await service.process_paritypay_callback(DummySession(), _callback()) is True
    finalize.assert_not_awaited()
    update.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_late_payment_after_expired_is_credited(monkeypatch: pytest.MonkeyPatch) -> None:
    """QR СБП оплатили после истечения: PAID поверх EXPIRED обязан зачислиться."""
    payment = FakeParityPayPayment(status='expired', processed_events=['9beea835-0937-4b5c-8f5a-c3a0d0e60346:EXPIRED'])
    _patch_crud(monkeypatch, payment)

    service = _make_service()
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_paritypay_payment', finalize, raising=False)

    assert await service.process_paritypay_callback(DummySession(), _callback()) is True
    assert payment.is_paid is True
    finalize.assert_awaited_once()


@pytest.mark.anyio('asyncio')
async def test_callback_error_status_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakeParityPayPayment()
    update = _patch_crud(monkeypatch, payment)

    service = _make_service()
    monkeypatch.setattr(service, '_finalize_paritypay_payment', AsyncMock(return_value=True), raising=False)

    assert await service.process_paritypay_callback(DummySession(), _callback(status='ERROR')) is True
    assert update.await_args.kwargs['status'] == 'declined'


@pytest.mark.anyio('asyncio')
async def test_refund_after_paid_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakeParityPayPayment(status='success', is_paid=True)
    update = _patch_crud(monkeypatch, payment)

    service = _make_service()
    assert await service.process_paritypay_callback(DummySession(), _callback(status='REFUNDED')) is True
    assert update.await_args.kwargs['status'] == 'refunded'


@pytest.mark.anyio('asyncio')
async def test_unknown_order_is_acknowledged(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_crud(monkeypatch, None)
    service = _make_service()

    assert await service.process_paritypay_callback(DummySession(), _callback()) is True


# ---------------------------------------------------------------------------
# Уведомления о подписках приходят на ТОТ ЖЕ адрес
# ---------------------------------------------------------------------------


@pytest.mark.anyio('asyncio')
async def test_subscription_notification_is_acknowledged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без 200 провайдер повторит доставку пять раз — подтверждаем и логируем."""
    payment = FakeParityPayPayment()
    update = _patch_crud(monkeypatch, payment)

    service = _make_service()
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_paritypay_payment', finalize, raising=False)

    subscription_event = {
        'id': 'aeca1ac8-72e3-408b-81e3-0a6028751e3f',
        'shop_subscription_id': 'order-1002',
        'shop_id': '874dfb1e-dbdb-4747-a1c0-005969725b74',
        'amount': '990.00',
        'interval': '1m',
        'status': 'active',
    }

    assert await service.process_paritypay_callback(DummySession(), subscription_event) is True
    finalize.assert_not_awaited()
    update.assert_not_awaited()
    assert payment.is_paid is False


@pytest.mark.anyio('asyncio')
async def test_subscription_charge_invoice_is_acknowledged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Списание по подписке: order_id вида "{sub_id}_2" нашему счёту не принадлежит."""
    payment = FakeParityPayPayment()
    update = _patch_crud(monkeypatch, payment)

    service = _make_service()
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_paritypay_payment', finalize, raising=False)

    body = _callback()
    body['order_id'] = 'aeca1ac8-72e3-408b-81e3-0a6028751e3f_2'
    body['subscription_id'] = 'aeca1ac8-72e3-408b-81e3-0a6028751e3f'

    assert await service.process_paritypay_callback(DummySession(), body) is True
    finalize.assert_not_awaited()
    update.assert_not_awaited()
    assert payment.is_paid is False


@pytest.mark.anyio('asyncio')
async def test_callback_missing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    assert await service.process_paritypay_callback(DummySession(), {'id': 'x'}) is False


def test_is_paritypay_enabled_requires_all_three_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'PARITYPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'PARITYPAY_SHOP_ID', 'shop', raising=False)
    monkeypatch.setattr(settings, 'PARITYPAY_SECRET_KEY', 'secret-1', raising=False)
    monkeypatch.setattr(settings, 'PARITYPAY_CALLBACK_SECRET', '', raising=False)

    assert settings.is_paritypay_enabled() is False

    monkeypatch.setattr(settings, 'PARITYPAY_CALLBACK_SECRET', 'secret-2', raising=False)
    assert settings.is_paritypay_enabled() is True


# ---------------------------------------------------------------------------
# check_paritypay_payment_status — страховка на случай потерянного уведомления
# ---------------------------------------------------------------------------


class StubInvoiceApi:
    """Заглушка чтения счёта: отдаёт заготовку либо бросает исключение."""

    def __init__(self, invoice: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.invoice = invoice
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def get_invoice(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.invoice


def _api_invoice(status: str = 'PAID', amount: Any = 1250.0) -> dict[str, Any]:
    return {
        'id': '9beea835-0937-4b5c-8f5a-c3a0d0e60346',
        'order_id': 'pp123_abcdef12',
        'amount': amount,
        'status': status,
        'link': 'https://pay.paritypay.net/9beea835',
    }


@pytest.mark.anyio('asyncio')
async def test_api_check_credits_paid_invoice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Уведомление потерялось — сверка обязана закрыть оплаченный счёт."""
    payment = FakeParityPayPayment(amount_kopeks=125000)
    payment.paritypay_payment_id = '9beea835-0937-4b5c-8f5a-c3a0d0e60346'
    _patch_crud(monkeypatch, payment)
    api = StubInvoiceApi(_api_invoice())
    monkeypatch.setattr(paritypay_mixin_module, 'paritypay_service', api)

    service = _make_service()
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_paritypay_payment', finalize, raising=False)

    result = await service.check_paritypay_payment_status(DummySession(), payment.order_id)

    assert result is not None
    assert payment.is_paid is True
    assert payment.status == 'success'
    finalize.assert_awaited_once()
    # Читаем по id процессинга, раз он известен
    assert api.calls[0]['invoice_id'] == '9beea835-0937-4b5c-8f5a-c3a0d0e60346'


@pytest.mark.anyio('asyncio')
async def test_api_check_falls_back_to_order_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если id процессинга не сохранился, ищем по своему order_id."""
    payment = FakeParityPayPayment()
    payment.paritypay_payment_id = None
    _patch_crud(monkeypatch, payment)
    api = StubInvoiceApi(_api_invoice(status='NEW'))
    monkeypatch.setattr(paritypay_mixin_module, 'paritypay_service', api)

    service = _make_service()
    await service.check_paritypay_payment_status(DummySession(), payment.order_id)

    assert api.calls[0]['order_id'] == payment.order_id
    assert api.calls[0]['invoice_id'] is None


@pytest.mark.anyio('asyncio')
async def test_api_check_amount_mismatch_blocks_credit(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakeParityPayPayment(amount_kopeks=125000)
    payment.paritypay_payment_id = 'x'
    update = _patch_crud(monkeypatch, payment)
    monkeypatch.setattr(paritypay_mixin_module, 'paritypay_service', StubInvoiceApi(_api_invoice(amount=1249.99)))

    service = _make_service()
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_paritypay_payment', finalize, raising=False)

    result = await service.check_paritypay_payment_status(DummySession(), payment.order_id)

    assert result['is_paid'] is False
    finalize.assert_not_awaited()
    assert update.await_args.kwargs['status'] == 'amount_mismatch'


@pytest.mark.anyio('asyncio')
async def test_api_check_unparseable_amount_blocks_credit(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakeParityPayPayment()
    payment.paritypay_payment_id = 'x'
    _patch_crud(monkeypatch, payment)
    monkeypatch.setattr(paritypay_mixin_module, 'paritypay_service', StubInvoiceApi(_api_invoice(amount='нет')))

    service = _make_service()
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_paritypay_payment', finalize, raising=False)

    result = await service.check_paritypay_payment_status(DummySession(), payment.order_id)

    assert result['is_paid'] is False
    finalize.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_api_check_syncs_non_paid_status(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakeParityPayPayment()
    payment.paritypay_payment_id = 'x'
    update = _patch_crud(monkeypatch, payment)
    monkeypatch.setattr(paritypay_mixin_module, 'paritypay_service', StubInvoiceApi(_api_invoice(status='EXPIRED')))

    service = _make_service()
    result = await service.check_paritypay_payment_status(DummySession(), payment.order_id)

    assert result['is_paid'] is False
    assert update.await_args.kwargs['status'] == 'expired'


@pytest.mark.anyio('asyncio')
async def test_api_check_handles_missing_invoice(monkeypatch: pytest.MonkeyPatch) -> None:
    """404 у провайдера — счёта нет, зачислять нечего."""
    payment = FakeParityPayPayment()
    payment.paritypay_payment_id = 'x'
    update = _patch_crud(monkeypatch, payment)
    monkeypatch.setattr(paritypay_mixin_module, 'paritypay_service', StubInvoiceApi(None))

    service = _make_service()
    result = await service.check_paritypay_payment_status(DummySession(), payment.order_id)

    assert result['is_paid'] is False
    update.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_api_check_survives_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Провайдер недоступен — возвращаем текущее состояние, а не падаем."""
    payment = FakeParityPayPayment()
    payment.paritypay_payment_id = 'x'
    _patch_crud(monkeypatch, payment)
    monkeypatch.setattr(
        paritypay_mixin_module, 'paritypay_service', StubInvoiceApi(error=ParityPayNetworkError('down'))
    )

    service = _make_service()
    result = await service.check_paritypay_payment_status(DummySession(), payment.order_id)

    assert result is not None
    assert result['is_paid'] is False
    assert payment.is_paid is False


@pytest.mark.anyio('asyncio')
async def test_api_check_skips_already_paid_and_final(monkeypatch: pytest.MonkeyPatch) -> None:
    """К провайдеру не ходим: платёж уже закрыт."""
    api = StubInvoiceApi(_api_invoice())
    monkeypatch.setattr(paritypay_mixin_module, 'paritypay_service', api)
    service = _make_service()

    paid = FakeParityPayPayment(status='success', is_paid=True)
    _patch_crud(monkeypatch, paid)
    assert (await service.check_paritypay_payment_status(DummySession(), paid.order_id))['is_paid'] is True

    refunded = FakeParityPayPayment(status='refunded')
    _patch_crud(monkeypatch, refunded)
    assert (await service.check_paritypay_payment_status(DummySession(), refunded.order_id))['is_paid'] is False

    assert api.calls == []


@pytest.mark.anyio('asyncio')
async def test_api_check_unknown_order_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_crud(monkeypatch, None)
    service = _make_service()

    assert await service.check_paritypay_payment_status(DummySession(), 'pp-unknown') is None
