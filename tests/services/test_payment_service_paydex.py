"""Тесты для сценариев Paydex в PaymentService.

Покрывают создание счёта (выбор способа, лимиты сумм, срок из ответа API),
обработку вебхука (зачисление, тестовый счёт, несовпадение суммы, идемпотентность,
стики терминальных статусов) и проверку HMAC-подписи вебхука.

Отдельно зафиксированы четыре отличия от соседних адаптеров, которые легко
потерять при рефакторинге: тестовый счёт не начисляет баланс, способ не
подставляется по умолчанию, срок жизни берётся из ответа API, а секрет вебхука
отделён от API-ключа.
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

import app.database.crud.paydex as paydex_crud_module
import app.services.payment.paydex as paydex_mixin_module
from app.config import settings
from app.services.paydex_service import PaydexService
from app.services.payment.paydex import resolve_paydex_method
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


class FakePaydexPayment:
    def __init__(
        self,
        *,
        status: str = 'pending',
        is_paid: bool = False,
        amount_kopeks: int = 50000,
    ) -> None:
        self.id = 11
        self.user_id = 77
        self.order_id = 'pdx123456_ab12cd'
        self.paydex_invoice_id = None
        self.amount_kopeks = amount_kopeks
        self.charged_amount_kopeks = None
        self.payment_method = None
        self.status = status
        self.is_paid = is_paid
        self.paid_at = None
        self.updated_at = None
        self.callback_payload = None
        self.metadata_json = {}
        self.transaction_id = None


class StubPaydexService:
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response or {
            'id': '0199ab00-7c11-71a2-9d4e-6f0b1e2d3c44',
            'orderId': 'echo',
            'status': 'pending',
            'amount': '500.00',
            'payableAmount': '520.00',
            'url': 'https://pay.paydex.pro/01999f0e-2a7b-7c3d-8e5f-112233445566',
            'expiresAt': '2026-07-19T10:30:00+00:00',
            'isTest': False,
        }
        self.calls: list[dict[str, Any]] = []

    async def create_invoice(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


def _make_service() -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    return service


def _enable_paydex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'PAYDEX_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'PAYDEX_API_KEY', 'sk_live_test', raising=False)
    monkeypatch.setattr(settings, 'PAYDEX_WEBHOOK_SECRET', 'whsec_test', raising=False)
    monkeypatch.setattr(settings, 'PAYDEX_MIN_AMOUNT_KOPEKS', 10000, raising=False)
    monkeypatch.setattr(settings, 'PAYDEX_MAX_AMOUNT_KOPEKS', 10000000, raising=False)
    monkeypatch.setattr(settings, 'PAYDEX_CURRENCY', 'RUB', raising=False)
    monkeypatch.setattr(settings, 'PAYDEX_PAYMENT_LIFETIME_MINUTES', 30, raising=False)
    monkeypatch.setattr(settings, 'PAYDEX_SBP_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'PAYDEX_CARD_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'PAYDEX_CRYPTO_ENABLED', True, raising=False)


def _patch_create_crud(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any] | None = None) -> None:
    async def fake_create_paydex_payment(**kwargs: Any) -> DummyLocalPayment:
        if captured is not None:
            captured.update(kwargs)
        return DummyLocalPayment(payment_id=999)

    monkeypatch.setattr(paydex_crud_module, 'create_paydex_payment', fake_create_paydex_payment)

    async def fake_get_user_by_id(_db: Any, _user_id: int) -> Any:
        class _User:
            telegram_id = 123456

        return _User()

    monkeypatch.setattr('app.services.payment_service.get_user_by_id', fake_get_user_by_id, raising=False)


def _patch_callback_crud(monkeypatch: pytest.MonkeyPatch, payment: FakePaydexPayment) -> AsyncMock:
    async def fake_get_by_order_id(_db: Any, _order_id: str) -> FakePaydexPayment:
        return payment

    async def fake_get_for_update(_db: Any, _payment_id: int) -> FakePaydexPayment:
        return payment

    update_mock = AsyncMock(return_value=payment)
    monkeypatch.setattr(paydex_crud_module, 'get_paydex_payment_by_order_id', fake_get_by_order_id)
    monkeypatch.setattr(paydex_crud_module, 'get_paydex_payment_by_id_for_update', fake_get_for_update)
    monkeypatch.setattr(paydex_crud_module, 'update_paydex_payment_status', update_mock)
    return update_mock


def _paid_webhook_payload(amount: str = '500.00', *, is_test: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'event': 'invoice.paid',
        'isTest': is_test,
        'data': {
            'invoice': {
                'id': '0199ab00-7c11-71a2-9d4e-6f0b1e2d3c44',
                'orderId': 'pdx123456_ab12cd',
                'status': 'paid',
                'amount': amount,
                'payableAmount': '520.00',
                'netAmount': '480.00',
                'feeAmount': '20.00',
                'method': 'sbp',
                'paidAt': '2026-07-19T10:05:00+00:00',
            }
        },
    }
    return payload


# ---------------------------------------------------------------------------
# create_paydex_payment
# ---------------------------------------------------------------------------


@pytest.mark.anyio('asyncio')
async def test_create_paydex_payment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_paydex(monkeypatch)
    stub = StubPaydexService()
    monkeypatch.setattr(paydex_mixin_module, 'paydex_service', stub)
    captured: dict[str, Any] = {}
    _patch_create_crud(monkeypatch, captured)

    service = _make_service()
    result = await service.create_paydex_payment(
        db=DummySession(),
        user_id=77,
        amount_kopeks=50000,
        description='Пополнение баланса',
    )

    assert result is not None
    assert result['local_payment_id'] == 999
    assert result['payment_url'] == 'https://pay.paydex.pro/01999f0e-2a7b-7c3d-8e5f-112233445566'
    assert result['amount_kopeks'] == 50000
    assert result['order_id'].startswith('pdx123456_')

    api_call = stub.calls[0]
    assert api_call['amount_kopeks'] == 50000
    assert api_call['customer_id'] == '123456'
    assert api_call['expire_minutes'] == 30
    # Способ покупатель выбирает на странице оплаты: включены все три.
    assert api_call['method'] is None
    # payableAmount — то, что заплатит покупатель вместе с комиссией.
    assert captured['charged_amount_kopeks'] == 52000
    # Срок — из ответа API, а не local now() + TTL.
    assert result['expires_at'].startswith('2026-07-19T10:30')


@pytest.mark.anyio('asyncio')
async def test_create_paydex_payment_explicit_sub_method(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_paydex(monkeypatch)
    stub = StubPaydexService()
    monkeypatch.setattr(paydex_mixin_module, 'paydex_service', stub)
    _patch_create_crud(monkeypatch)

    service = _make_service()
    result = await service.create_paydex_payment(
        db=DummySession(),
        user_id=77,
        amount_kopeks=50000,
        payment_method_type='crypto',
    )

    assert result is not None
    assert stub.calls[0]['method'] == 'crypto'


def test_resolve_paydex_method_single_enabled_is_fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Включён ровно один способ — не гоняем покупателя через лишний экран выбора."""
    # Sub-методы читаются только у включённого провайдера (is_paydex_*_enabled
    # требует и ключей), поэтому шлюз включаем целиком.
    _enable_paydex(monkeypatch)
    monkeypatch.setattr(settings, 'PAYDEX_SBP_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'PAYDEX_CARD_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'PAYDEX_CRYPTO_ENABLED', False, raising=False)

    assert resolve_paydex_method(None) == 'sbp'


def test_resolve_paydex_method_never_guesses_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """Карта может быть выключена в проекте — подставлять её «по умолчанию» нельзя."""
    # Sub-методы читаются только у включённого провайдера (is_paydex_*_enabled
    # требует и ключей), поэтому шлюз включаем целиком.
    _enable_paydex(monkeypatch)
    monkeypatch.setattr(settings, 'PAYDEX_SBP_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'PAYDEX_CARD_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'PAYDEX_CRYPTO_ENABLED', False, raising=False)

    assert resolve_paydex_method(None) is None
    assert resolve_paydex_method('sbp') == 'sbp'


@pytest.mark.anyio('asyncio')
async def test_create_paydex_payment_respects_amount_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_paydex(monkeypatch)
    monkeypatch.setattr(paydex_mixin_module, 'paydex_service', StubPaydexService())

    service = _make_service()
    result_low = await service.create_paydex_payment(db=DummySession(), user_id=77, amount_kopeks=9999)
    result_high = await service.create_paydex_payment(db=DummySession(), user_id=77, amount_kopeks=10000001)

    assert result_low is None
    assert result_high is None


@pytest.mark.anyio('asyncio')
async def test_create_paydex_payment_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'PAYDEX_ENABLED', False, raising=False)
    service = _make_service()
    assert await service.create_paydex_payment(db=DummySession(), user_id=77, amount_kopeks=50000) is None


# ---------------------------------------------------------------------------
# process_paydex_callback
# ---------------------------------------------------------------------------


@pytest.mark.anyio('asyncio')
async def test_process_paydex_callback_paid_finalizes(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakePaydexPayment()
    _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_paydex_payment', finalize_mock, raising=False)

    result = await service.process_paydex_callback(DummySession(), _paid_webhook_payload())

    assert result is True
    assert payment.is_paid is True
    assert payment.status == 'success'
    assert payment.paydex_invoice_id == '0199ab00-7c11-71a2-9d4e-6f0b1e2d3c44'
    assert payment.charged_amount_kopeks == 52000
    assert payment.payment_method == 'sbp'
    finalize_mock.assert_awaited_once()


@pytest.mark.anyio('asyncio')
async def test_process_paydex_callback_test_invoice_never_credits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Счёт с тестового ключа «оплачивается» эмулятором без денег — баланс не трогаем."""
    payment = FakePaydexPayment()
    update_mock = _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_paydex_payment', finalize_mock, raising=False)

    result = await service.process_paydex_callback(DummySession(), _paid_webhook_payload(is_test=True))

    assert result is True  # 2xx: повторять доставку бессмысленно
    finalize_mock.assert_not_awaited()
    assert payment.is_paid is False
    assert update_mock.await_args.kwargs['status'] == 'error'
    assert update_mock.await_args.kwargs['is_paid'] is False


@pytest.mark.anyio('asyncio')
async def test_process_paydex_callback_amount_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakePaydexPayment(amount_kopeks=50000)
    update_mock = _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_paydex_payment', finalize_mock, raising=False)

    result = await service.process_paydex_callback(DummySession(), _paid_webhook_payload(amount='499.99'))

    assert result is False
    finalize_mock.assert_not_awaited()
    assert update_mock.await_args.kwargs['status'] == 'amount_mismatch'
    assert update_mock.await_args.kwargs['is_paid'] is False


@pytest.mark.anyio('asyncio')
async def test_process_paydex_callback_missing_amount_does_not_credit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """paid без amount: сверять нечего, платёж остаётся pending под ретрай и фоновую сверку."""
    payment = FakePaydexPayment()
    update_mock = _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_paydex_payment', finalize_mock, raising=False)

    payload = _paid_webhook_payload()
    del payload['data']['invoice']['amount']
    result = await service.process_paydex_callback(DummySession(), payload)

    assert result is False  # не-2xx -> Paydex повторит вебхук
    finalize_mock.assert_not_awaited()
    update_mock.assert_not_awaited()
    assert payment.is_paid is False
    assert payment.status == 'pending'


@pytest.mark.anyio('asyncio')
async def test_process_paydex_callback_already_paid_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakePaydexPayment(status='success', is_paid=True)
    _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_paydex_payment', finalize_mock, raising=False)

    result = await service.process_paydex_callback(DummySession(), _paid_webhook_payload())

    assert result is True
    finalize_mock.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_process_paydex_callback_sticky_terminal_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Провайдер не может «починить» отклонённый платёж повторным вебхуком."""
    payment = FakePaydexPayment(status='declined')
    update_mock = _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_paydex_payment', finalize_mock, raising=False)

    result = await service.process_paydex_callback(DummySession(), _paid_webhook_payload())

    assert result is True
    finalize_mock.assert_not_awaited()
    update_mock.assert_not_awaited()
    assert payment.is_paid is False


@pytest.mark.anyio('asyncio')
async def test_process_paydex_callback_non_paid_status_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = FakePaydexPayment()
    update_mock = _patch_callback_crud(monkeypatch, payment)

    service = _make_service()
    finalize_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_paydex_payment', finalize_mock, raising=False)

    payload = _paid_webhook_payload()
    payload['event'] = 'invoice.expired'
    payload['data']['invoice']['status'] = 'expired'
    result = await service.process_paydex_callback(DummySession(), payload)

    assert result is True
    finalize_mock.assert_not_awaited()
    assert update_mock.await_args.kwargs['status'] == 'expired'


@pytest.mark.anyio('asyncio')
async def test_process_paydex_callback_missing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    assert await service.process_paydex_callback(DummySession(), {'event': 'invoice.paid'}) is False


# ---------------------------------------------------------------------------
# verify_webhook_signature / предикаты включения
# ---------------------------------------------------------------------------


def test_verify_webhook_signature_accepts_both_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'PAYDEX_WEBHOOK_SECRET', 'whsec_test', raising=False)
    service = PaydexService()
    body = b'{"event":"invoice.paid"}'
    digest = hmac.new(b'whsec_test', body, hashlib.sha256).hexdigest()

    assert service.verify_webhook_signature(body, f'sha256={digest}') is True
    assert service.verify_webhook_signature(body, digest) is True
    assert service.verify_webhook_signature(body, f'sha256={digest[:-1]}0') is False
    assert service.verify_webhook_signature(body, None) is False
    assert service.verify_webhook_signature(b'{"event":"other"}', f'sha256={digest}') is False


def test_verify_webhook_signature_blank_secret_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без секрета HMAC считался бы от пустого ключа — подпись подделал бы кто угодно."""
    monkeypatch.setattr(settings, 'PAYDEX_WEBHOOK_SECRET', '', raising=False)
    service = PaydexService()
    body = b'{"event":"invoice.paid"}'
    digest = hmac.new(b'', body, hashlib.sha256).hexdigest()

    assert service.verify_webhook_signature(body, f'sha256={digest}') is False


def test_is_paydex_enabled_requires_both_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'PAYDEX_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'PAYDEX_API_KEY', 'sk_live_test', raising=False)
    monkeypatch.setattr(settings, 'PAYDEX_WEBHOOK_SECRET', '', raising=False)

    assert settings.is_paydex_enabled() is False

    monkeypatch.setattr(settings, 'PAYDEX_WEBHOOK_SECRET', 'whsec_test', raising=False)
    assert settings.is_paydex_enabled() is True
    # Инвариант проекта: enabled == флаг and configured
    assert settings.is_paydex_configured() is True


def test_bot_handler_returns_payer_to_the_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Из банка покупатель должен попасть обратно в бот, а не на домен проекта в Paydex."""
    from app.handlers.balance.paydex import _bot_return_url

    monkeypatch.setattr(settings, 'BOT_USERNAME', 'MyShopBot', raising=False)
    assert _bot_return_url() == 'https://t.me/MyShopBot'

    monkeypatch.setattr(settings, 'BOT_USERNAME', None, raising=False)
    assert _bot_return_url() is None
