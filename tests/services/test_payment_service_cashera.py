"""Cashera: создание платежа, вебхук, сверка через API и клиент.

Зачисление проверяется сквозным путём на SQLite: реальный пользователь, баланс
и транзакция, а не моки CRUD — именно там живут двойные зачисления.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from sqlalchemy import func, select

import app.services.payment.cashera as cashera_mixin_module
from app.config import settings
from app.database.models import Base, CasheraPayment, PaymentMethod, Transaction, User
from app.services.cashera_service import CasheraAPIError, CasheraService, normalize_payment_url
from app.services.payment_service import PaymentService
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)


def _enable(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    values = {
        'CASHERA_ENABLED': True,
        'CASHERA_API_KEY': 'pk_test',
        'CASHERA_API_SECRET': 'sk_test',
        'CASHERA_ACTIVE_METHODS': 'sbp,card',
        'CASHERA_MIN_AMOUNT_KOPEKS': 10000,
        'CASHERA_MAX_AMOUNT_KOPEKS': 10000000,
        'WEBHOOK_URL': 'https://bot.example.com',
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value, raising=False)


class StubCashera:
    def __init__(self, response: dict[str, Any] | None = None, remote: dict[str, Any] | None = None) -> None:
        self.response = response
        self.remote = remote
        self.calls: list[dict[str, Any]] = []

    async def create_transaction(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response or {
            'uuid': '9b1f2c4e-7a01-4b9d-8f1c-2eab57d90c11',
            'status': 'pending',
            'payment_method': kwargs.get('payment_method'),
            'payment_url': 'pay.cashera.cash/9b1f2c4e',
            'expires_at': '2026-06-02T18:20:00+00:00',
        }

    async def get_transaction(self, _uuid: str) -> dict[str, Any]:
        return self.remote or {}

    async def get_transaction_by_external_id(self, _external_id: str) -> dict[str, Any]:
        return self.remote or {}


def _service() -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    return service


async def _seed_user(db) -> User:
    user = User(telegram_id=555, username='payer', language='ru', balance_kopeks=0)
    db.add(user)
    await db.commit()
    return user


async def _create(db, monkeypatch, stub: StubCashera, *, method: str | None = 'sbp', amount: int = 49900):
    monkeypatch.setattr(cashera_mixin_module, 'cashera_service', stub)
    user = await _seed_user(db)
    user_id = user.id  # после rollback объект экспирируется — держим id отдельно
    result = await _service().create_cashera_payment(
        db, user_id=user_id, amount_kopeks=amount, payment_method_code=method
    )
    return user_id, result


def _event(order_id: str, *, status: str = 'paid', amount: Any = 49900, currency: str = 'RUB') -> dict[str, Any]:
    return {
        'event': 'transaction.status_updated',
        'transaction': {
            'uuid': '9b1f2c4e-7a01-4b9d-8f1c-2eab57d90c11',
            'external_id': order_id,
            'status': status,
            'amount': amount,
            'currency': currency,
            'payment_method': 'sbp',
            'paid_at': '2026-06-02T18:11:42+00:00',
        },
    }


async def _balance(db, user_id: int) -> int:
    await db.rollback()
    return (await db.execute(select(User.balance_kopeks).where(User.id == user_id))).scalar_one()


async def _deposits(db) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.payment_method == PaymentMethod.CASHERA.value)
        )
    ).scalar_one()


# --- создание ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_sends_spec_payload_and_stores_payment(monkeypatch):
    _enable(monkeypatch)
    stub = StubCashera()
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, stub)
        payment = (await db.execute(select(CasheraPayment))).scalar_one()

    call = stub.calls[0]
    assert call['amount_kopeks'] == 49900
    assert call['payment_method'] == 'sbp'
    assert call['callback_url'] == 'https://bot.example.com/cashera-webhook'
    # external_id Cashera: только буквы, цифры и . _ -
    assert re.fullmatch(r'[A-Za-z0-9._-]{1,255}', call['external_id'])
    assert payment.order_id == call['external_id']
    assert payment.cashera_uuid == '9b1f2c4e-7a01-4b9d-8f1c-2eab57d90c11'
    # payment_url в ответе без схемы — ссылка должна открываться
    assert result['payment_url'] == 'https://pay.cashera.cash/9b1f2c4e'


@pytest.mark.asyncio
async def test_create_rejects_disabled_method_and_limits(monkeypatch):
    _enable(monkeypatch)
    stub = StubCashera()
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, not_enabled = await _create(db, monkeypatch, stub, method='crypto')
        too_small = await _service().create_cashera_payment(
            db, user_id=user_id, amount_kopeks=5000, payment_method_code='sbp'
        )

    assert not_enabled is None
    assert too_small is None
    assert stub.calls == []


def test_blank_secret_means_disabled(monkeypatch):
    """С пустым секретом вебхук подделал бы кто угодно — шлюз считается выключенным."""
    _enable(monkeypatch, CASHERA_API_SECRET='')
    assert settings.is_cashera_enabled() is False


def test_unknown_method_codes_are_dropped(monkeypatch):
    _enable(monkeypatch, CASHERA_ACTIVE_METHODS='card, bogus ,SBP,card')
    assert settings.get_cashera_active_methods() == ['card', 'sbp']


# --- вебхук -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paid_webhook_credits_once_and_replay_is_ignored(monkeypatch):
    _enable(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, StubCashera())
        service = _service()

        assert await service.process_cashera_webhook(db, _event(result['order_id'])) is True
        assert await _balance(db, user_id) == 49900

        # Тот же uuid + status пришёл повторно — второй раз не зачисляем.
        assert await service.process_cashera_webhook(db, _event(result['order_id'])) is True
        assert await _balance(db, user_id) == 49900
        assert await _deposits(db) == 1


@pytest.mark.asyncio
async def test_amount_or_currency_mismatch_is_not_credited(monkeypatch):
    _enable(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, StubCashera())

        # Повтор не исправит расхождение — подтверждаем (2xx), но не зачисляем.
        assert await _service().process_cashera_webhook(db, _event(result['order_id'], amount=100)) is True
        payment = (await db.execute(select(CasheraPayment))).scalar_one()

        assert payment.status == 'amount_mismatch'
        assert await _balance(db, user_id) == 0

        # И поздний «правильный» вебхук уже не зачисляет платёж из финального статуса.
        assert await _service().process_cashera_webhook(db, _event(result['order_id'])) is True
        assert await _balance(db, user_id) == 0


@pytest.mark.asyncio
async def test_wrong_currency_is_mismatch(monkeypatch):
    _enable(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, StubCashera())
        await _service().process_cashera_webhook(db, _event(result['order_id'], currency='USD'))
        assert await _balance(db, user_id) == 0


@pytest.mark.asyncio
async def test_paid_without_amount_asks_for_retry(monkeypatch):
    """Без подтверждённой суммы не зачисляем и отвечаем 5xx — Cashera повторит."""
    _enable(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, StubCashera())
        assert await _service().process_cashera_webhook(db, _event(result['order_id'], amount=None)) is False
        assert await _balance(db, user_id) == 0


@pytest.mark.asyncio
async def test_failed_status_is_final(monkeypatch):
    _enable(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, StubCashera())
        service = _service()
        assert await service.process_cashera_webhook(db, _event(result['order_id'], status='failed')) is True
        assert await service.process_cashera_webhook(db, _event(result['order_id'])) is True

        payment = (await db.execute(select(CasheraPayment))).scalar_one()
        assert payment.status == 'failed'
        assert await _balance(db, user_id) == 0


@pytest.mark.asyncio
async def test_refund_after_credit_debits_balance_once(monkeypatch):
    _enable(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, StubCashera())
        service = _service()
        await service.process_cashera_webhook(db, _event(result['order_id']))
        assert await _balance(db, user_id) == 49900

        assert await service.process_cashera_webhook(db, _event(result['order_id'], status='refunded')) is True
        assert await _balance(db, user_id) == 0

        # Повтор и последующий чарджбэк по тому же платежу второй раз не списывают.
        await service.process_cashera_webhook(db, _event(result['order_id'], status='refunded'))
        await service.process_cashera_webhook(db, _event(result['order_id'], status='chargeback'))
        assert await _balance(db, user_id) == 0

        payment = (await db.execute(select(CasheraPayment))).scalar_one()
        assert payment.cashera_status == 'chargeback'
        assert payment.metadata_json['reversal']['debited_kopeks'] == 49900
        withdrawals = (
            await db.execute(select(func.count()).select_from(Transaction).where(Transaction.type == 'withdrawal'))
        ).scalar_one()
        assert withdrawals == 1


@pytest.mark.asyncio
async def test_chargeback_after_balance_was_spent_records_shortfall(monkeypatch):
    """Отрицательного баланса нет: списываем сколько есть, недостачу — в платёж и тревогу."""
    _enable(monkeypatch)
    alerts = []
    monkeypatch.setattr(cashera_mixin_module.alert_logger, 'error', lambda *a, **kw: alerts.append(kw))
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, StubCashera())
        service = _service()
        await service.process_cashera_webhook(db, _event(result['order_id']))

        user = await db.get(User, user_id)
        user.balance_kopeks = 10000  # остальное уже потрачено
        await db.commit()

        await service.process_cashera_webhook(db, _event(result['order_id'], status='chargeback'))

        assert await _balance(db, user_id) == 0
        payment = (await db.execute(select(CasheraPayment))).scalar_one()
        assert payment.metadata_json['reversal']['debited_kopeks'] == 10000
        assert payment.metadata_json['reversal']['shortfall_kopeks'] == 39900
    assert alerts and alerts[-1]['shortfall_kopeks'] == 39900


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'payload',
    [
        {'event': 'webhook.test', 'test': {'sent_at': '2026-06-02T18:00:00+00:00'}},
        {'event': 'payout.status_updated', 'payout': {'uuid': 'x', 'status': 'completed'}},
        {'event': 'transaction.status_updated', 'transaction': {'external_id': 'not-ours', 'status': 'paid'}},
    ],
)
async def test_foreign_and_test_events_are_acknowledged(monkeypatch, payload):
    _enable(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        assert await _service().process_cashera_webhook(db, payload) is True


# --- сверка через API -------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_check_credits_when_webhook_was_lost(monkeypatch):
    _enable(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        stub = StubCashera()
        user_id, result = await _create(db, monkeypatch, stub)
        stub.remote = _event(result['order_id'])['transaction']

        status = await _service().check_cashera_payment_status(db, result['order_id'])

        assert status['is_paid'] is True
        assert await _balance(db, user_id) == 49900


# --- клиент -----------------------------------------------------------------------


def test_normalize_payment_url():
    assert normalize_payment_url('pay.cashera.cash/abc') == 'https://pay.cashera.cash/abc'
    assert normalize_payment_url('https://pay.cashera.cash/abc') == 'https://pay.cashera.cash/abc'
    assert normalize_payment_url('') is None
    assert normalize_payment_url(None) is None


def test_verify_webhook(monkeypatch):
    _enable(monkeypatch)
    service = CasheraService()
    assert service.verify_webhook('pk_test', 'sk_test') is True
    assert service.verify_webhook('pk_test', 'sk_wrong') is False
    assert service.verify_webhook('pk_other', 'sk_test') is False
    assert service.verify_webhook(None, None) is False


def test_verify_webhook_fails_closed_without_secret(monkeypatch):
    _enable(monkeypatch, CASHERA_API_SECRET='')
    assert CasheraService().verify_webhook('pk_test', '') is False


class _FakeResponse:
    def __init__(self, status: int, data: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._data = data
        self.headers = headers or {}

    async def json(self, content_type=None):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = responses
        self.calls = 0
        self.closed = False

    def request(self, *_args, **_kwargs):
        response = self.responses[self.calls]
        self.calls += 1
        return response


@pytest.mark.asyncio
async def test_client_retries_5xx_and_429_then_succeeds(monkeypatch):
    _enable(monkeypatch)
    service = CasheraService()
    session = _FakeSession(
        [
            _FakeResponse(502, {'message': 'provider'}),
            _FakeResponse(429, {'message': 'slow down'}, {'Retry-After': '0'}),
            _FakeResponse(201, {'uuid': 'u-1', 'status': 'pending'}),
        ]
    )
    service._session = session  # type: ignore[assignment]
    monkeypatch.setattr('app.services.cashera_service.asyncio.sleep', _no_sleep)

    data = await service.create_transaction(
        amount_kopeks=49900, external_id='cas1_x', description='d', payment_method='sbp'
    )

    assert data['uuid'] == 'u-1'
    assert session.calls == 3


@pytest.mark.asyncio
async def test_client_does_not_retry_validation_errors(monkeypatch):
    _enable(monkeypatch)
    service = CasheraService()
    session = _FakeSession(
        [
            _FakeResponse(
                422, {'message': 'invalid', 'errors': {'payment_method': ['The selected payment method is invalid.']}}
            )
        ]
    )
    service._session = session  # type: ignore[assignment]

    with pytest.raises(CasheraAPIError) as error:
        await service.create_transaction(
            amount_kopeks=49900, external_id='cas1_x', description='d', payment_method='sbp'
        )

    assert error.value.status_code == 422
    assert 'payment_method' in error.value.errors
    assert session.calls == 1


async def _no_sleep(_seconds: float) -> None:
    return None


# --- H2H: свой экран оплаты -------------------------------------------------------


class _H2HStub:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def get_h2h(self, _uuid):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.mark.asyncio
async def test_h2h_retries_until_requisites_are_ready(monkeypatch):
    _enable(monkeypatch, CASHERA_H2H_ENABLED=True)
    stub = _H2HStub([CasheraAPIError(422, 'not ready'), {'qr': 'https://qr.nspk.ru/AS1', 'amount': 499}])
    monkeypatch.setattr(cashera_mixin_module, 'cashera_service', stub)
    monkeypatch.setattr('asyncio.sleep', _no_sleep)

    h2h = await _service().get_cashera_h2h('u-1', 'sbp')

    assert h2h == {'qr': 'https://qr.nspk.ru/AS1', 'amount': 499}
    assert stub.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('enabled', 'method'),
    [(False, 'sbp'), (True, 'mastercard'), (True, 'cryptobot')],
)
async def test_h2h_not_requested_when_off_or_unsupported(monkeypatch, enabled, method):
    """mastercard и cryptobot у Cashera только ссылкой; при выключенной настройке — тоже."""
    _enable(monkeypatch, CASHERA_H2H_ENABLED=enabled)
    stub = _H2HStub([{'qr': 'x'}])
    monkeypatch.setattr(cashera_mixin_module, 'cashera_service', stub)

    assert await _service().get_cashera_h2h('u-1', method) is None
    assert stub.calls == 0


@pytest.mark.asyncio
async def test_h2h_failure_falls_back_to_link(monkeypatch):
    _enable(monkeypatch, CASHERA_H2H_ENABLED=True)
    monkeypatch.setattr(cashera_mixin_module, 'cashera_service', _H2HStub([CasheraAPIError(502, 'provider')]))
    assert await _service().get_cashera_h2h('u-1', 'card') is None
