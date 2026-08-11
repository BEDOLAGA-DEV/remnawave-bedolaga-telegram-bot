"""Интеграционные проверки обработки вебхуков PaymentService."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.services.payment.cryptobot as cryptobot_module
import app.services.payment_service as payment_service_module
from app.config import settings
from app.database.models import PaymentMethod
from app.services.payment_service import PaymentService


class DummyBot:
    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []

    async def send_message(
        self, *args: Any, **kwargs: Any
    ) -> None:  # pragma: no cover - бизнес-логика тестируется через вызов
        self.sent_messages.append({'args': args, 'kwargs': kwargs})


class FakeScalarResult:
    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)

    def all(self) -> list[Any]:  # pragma: no cover - утилитарный метод
        return list(self._items)

    def first(self) -> Any:  # pragma: no cover - утилитарный метод
        return self._items[0] if self._items else None

    def one(self) -> Any:  # pragma: no cover - утилитарный метод
        if len(self._items) != 1:
            raise ValueError('Expected exactly one result')
        return self._items[0]

    def one_or_none(self) -> Any:  # pragma: no cover - утилитарный метод
        if not self._items:
            return None
        if len(self._items) > 1:
            raise ValueError('Expected zero or one result')
        return self._items[0]

    def __iter__(self):  # pragma: no cover - утилитарный метод
        return iter(self._items)


class FakeResult:
    def __init__(self, value: Any = None) -> None:
        self._value = value

    def _as_iterable(self) -> list[Any]:
        if isinstance(self._value, list):
            return self._value
        if self._value is None:
            return []
        return [self._value]

    def scalar(self) -> Any:
        items = self._as_iterable()
        return items[0] if items else None

    def scalar_one_or_none(self) -> Any:
        items = self._as_iterable()
        if not items:
            return None
        if len(items) > 1:
            raise ValueError('Expected zero or one result')
        return items[0]

    def scalar_one(self) -> Any:
        items = self._as_iterable()
        if len(items) != 1:
            raise ValueError('Expected exactly one result')
        return items[0]

    def first(self) -> Any:  # pragma: no cover - утилитарный метод
        items = self._as_iterable()
        return items[0] if items else None

    def all(self) -> list[Any]:  # pragma: no cover - утилитарный метод
        return list(self._as_iterable())

    def one_or_none(self) -> Any:  # pragma: no cover - утилитарный метод
        items = self._as_iterable()
        if not items:
            return None
        if len(items) > 1:
            raise ValueError('Expected zero or one result')
        return items[0]

    def scalars(self) -> FakeScalarResult:  # pragma: no cover - утилитарный метод
        return FakeScalarResult(self._as_iterable())


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.refreshed: list[Any] = []
        self.added: list[Any] = []
        self.execute_statements: list[Any] = []
        self.execute_results: list[Any] = []
        # Optional opt-in routing: maps an entity-class-name substring to the
        # object a matching `select(Entity)...` statement should resolve to.
        # Lets ORM-level lock/select calls (e.g. YooKassaPayment FOR UPDATE,
        # User eager-load) return the right test object regardless of call order.
        self.route_by_entity: dict[str, Any] | None = None

    def _route(self, statement: Any) -> Any | None:
        if not self.route_by_entity:
            return None
        try:
            descriptions = statement.column_descriptions
        except Exception:
            return None
        for description in descriptions:
            entity = description.get('entity')
            entity_name = getattr(entity, '__name__', '')
            for key, value in self.route_by_entity.items():
                if key in entity_name:
                    return value
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def flush(self) -> None:
        return None

    async def rollback(self) -> None:  # pragma: no cover
        return None

    async def refresh(self, obj: Any) -> None:
        self.refreshed.append(obj)

    def add(self, obj: Any) -> None:  # pragma: no cover - используется при создании транзакций
        self.added.append(obj)

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> FakeResult:
        self.execute_statements.append(statement)
        routed = self._route(statement)
        if routed is not None:
            return routed if isinstance(routed, FakeResult) else FakeResult(routed)
        if self.execute_results:
            result = self.execute_results.pop(0)
            if callable(result):  # pragma: no cover - гибкость для будущих тестов
                result = result(statement, *args, **kwargs)
        else:
            result = None

        if isinstance(result, FakeResult):
            return result

        return FakeResult(result)


def _make_service(bot: DummyBot) -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = bot
    service.yookassa_service = None
    service.stars_service = None
    service.mulenpay_service = None
    service.pal24_service = None
    service.cryptobot_service = None
    service.heleket_service = None
    return service


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.mark.anyio('asyncio')
@pytest.mark.parametrize('status_field', ['payment_status', 'status', 'paymentStatus'])
async def test_process_mulenpay_callback_success(monkeypatch: pytest.MonkeyPatch, status_field: str) -> None:
    bot = DummyBot()
    service = _make_service(bot)
    fake_session = FakeSession()
    payment = SimpleNamespace(
        id=1,
        uuid='mulen_uuid',
        mulen_payment_id=123,
        amount_kopeks=5000,
        user_id=42,
        transaction_id=None,
        is_paid=False,
    )

    async def fake_get_by_uuid(db, uuid):
        return payment

    async def fake_get_by_id(db, mid):
        return None

    async def fake_get_mulenpay_for_update(db, pid):
        return payment

    monkeypatch.setattr(payment_service_module, 'get_mulenpay_payment_by_uuid', fake_get_by_uuid)
    monkeypatch.setattr(payment_service_module, 'get_mulenpay_payment_by_mulen_id', fake_get_by_id)

    mulen_module = ModuleType('app.database.crud.mulenpay')
    mulen_module.get_mulenpay_payment_by_id_for_update = fake_get_mulenpay_for_update
    monkeypatch.setitem(sys.modules, 'app.database.crud.mulenpay', mulen_module)

    transactions: list[dict[str, Any]] = []

    async def fake_create_transaction(db, **kwargs):
        transactions.append(kwargs)
        return SimpleNamespace(id=777, **kwargs)

    monkeypatch.setattr(payment_service_module, 'create_transaction', fake_create_transaction)

    updated_status: dict[str, Any] = {}

    async def fake_update_status(db, payment=None, status=None, **kwargs):
        payment.status = status
        payment.is_paid = status == 'success'
        updated_status.update({'status': status, 'kwargs': kwargs})

    monkeypatch.setattr(payment_service_module, 'update_mulenpay_payment_status', fake_update_status)

    async def fake_link(db, payment=None, transaction_id=None):
        payment.transaction_id = transaction_id

    monkeypatch.setattr(payment_service_module, 'link_mulenpay_payment_to_transaction', fake_link)

    user = SimpleNamespace(
        id=42,
        telegram_id=100500,
        balance_kopeks=0,
        has_made_first_topup=False,
        promo_group=None,
        subscription=None,
        referred_by_id=None,
        referrer=None,
    )
    user.get_primary_promo_group = lambda: getattr(user, 'promo_group', None)

    async def fake_get_user(db, user_id):
        return user

    monkeypatch.setattr(payment_service_module, 'get_user_by_id', fake_get_user)
    monkeypatch.setattr(type(settings), 'format_price', lambda self, amount: f'{amount / 100:.2f}₽', raising=False)

    async def fake_lock_user(db, locked_user):
        return locked_user

    monkeypatch.setattr('app.database.crud.user.lock_user_for_update', fake_lock_user)
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())

    referral_mock = SimpleNamespace(process_referral_topup=AsyncMock())
    monkeypatch.setitem(sys.modules, 'app.services.referral_service', referral_mock)

    class DummyAdminService:
        def __init__(self, bot):
            self.bot = bot
            self.calls: list[Any] = []

        async def send_balance_topup_notification(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    admin_service = DummyAdminService(bot)
    monkeypatch.setitem(
        sys.modules,
        'app.services.admin_notification_service',
        SimpleNamespace(AdminNotificationService=lambda bot: admin_service),
    )

    service.build_topup_success_keyboard = AsyncMock(return_value=None)

    payload = {
        'uuid': 'mulen_uuid',
        'id': 123,
        'amount': '50.00',
    }
    payload[status_field] = 'success'

    result = await service.process_mulenpay_callback(fake_session, payload)

    assert result is True
    assert transactions and transactions[0]['user_id'] == 42
    assert payment.transaction_id == 777
    # Success path now marks the locked payment row inline (no separate status-CRUD call).
    assert payment.status == 'success'
    assert payment.is_paid is True
    assert user.balance_kopeks == 5000
    assert fake_session.commits >= 1
    assert bot.sent_messages  # сообщение пользователю отправлено


@pytest.mark.anyio('asyncio')
async def test_process_cryptobot_webhook_success(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = DummyBot()
    service = _make_service(bot)
    fake_session = FakeSession()
    payment = SimpleNamespace(
        invoice_id='inv_1',
        user_id=7,
        status='pending',
        transaction_id=None,
        amount='12.50',
        asset='USDT',
        amount_float=12.5,
    )

    async def fake_get_crypto(db, invoice_id):
        return payment

    async def fake_update_status(db, invoice_id, status, paid_at):
        payment.status = status
        payment.paid_at = paid_at
        return payment

    async def fake_link(db, invoice_id, transaction_id):
        payment.transaction_id = transaction_id

    fake_cryptobot_module = ModuleType('app.database.crud.cryptobot')
    fake_cryptobot_module.get_cryptobot_payment_by_invoice_id = fake_get_crypto
    fake_cryptobot_module.get_cryptobot_payment_by_invoice_id_for_update = fake_get_crypto
    fake_cryptobot_module.update_cryptobot_payment_status = fake_update_status
    fake_cryptobot_module.link_cryptobot_payment_to_transaction = fake_link
    monkeypatch.setitem(sys.modules, 'app.database.crud.cryptobot', fake_cryptobot_module)

    transactions: list[dict[str, Any]] = []
    created_transaction: SimpleNamespace | None = None

    async def fake_create_transaction(db, **kwargs):
        nonlocal created_transaction
        transactions.append(kwargs)
        created_transaction = SimpleNamespace(id=888, **kwargs)
        return created_transaction

    fake_transaction_module = ModuleType('app.database.crud.transaction')
    fake_transaction_module.create_transaction = fake_create_transaction

    async def fake_get_transaction_by_id(db, transaction_id):
        return created_transaction

    fake_transaction_module.get_transaction_by_id = fake_get_transaction_by_id

    async def fake_emit_side_effects(db, transaction, **kwargs):
        return None

    fake_transaction_module.emit_transaction_side_effects = fake_emit_side_effects
    monkeypatch.setitem(sys.modules, 'app.database.crud.transaction', fake_transaction_module)
    monkeypatch.setattr(payment_service_module, 'create_transaction', fake_create_transaction)

    user = SimpleNamespace(
        id=7,
        telegram_id=700,
        balance_kopeks=0,
        has_made_first_topup=False,
        promo_group=None,
        subscription=None,
        referred_by_id=None,
        referrer=None,
    )
    user.get_primary_promo_group = lambda: getattr(user, 'promo_group', None)

    async def fake_get_user_crypto(db, user_id):
        return user

    monkeypatch.setattr(payment_service_module, 'get_user_by_id', fake_get_user_crypto)

    async def fake_lock_user(db, user):
        return user

    fake_user_module = ModuleType('app.database.crud.user')
    fake_user_module.get_user_by_id = fake_get_user_crypto
    fake_user_module.lock_user_for_update = fake_lock_user
    monkeypatch.setitem(sys.modules, 'app.database.crud.user', fake_user_module)

    referral_crypto = SimpleNamespace(process_referral_topup=AsyncMock())
    monkeypatch.setitem(sys.modules, 'app.services.referral_service', referral_crypto)

    admin_calls: list[Any] = []

    class DummyAdminService2:
        def __init__(self, bot):
            self.bot = bot

        async def send_balance_topup_notification(self, *args, **kwargs):
            admin_calls.append((args, kwargs))

    monkeypatch.setitem(
        sys.modules,
        'app.services.admin_notification_service',
        SimpleNamespace(AdminNotificationService=DummyAdminService2),
    )

    class DummyAsyncSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def rollback(self):  # pragma: no cover - defensive stub
            return None

    monkeypatch.setattr(cryptobot_module, 'AsyncSessionLocal', DummyAsyncSession)
    monkeypatch.setattr(payment_service_module.currency_converter, 'usd_to_rub', AsyncMock(return_value=140.0))
    monkeypatch.setattr(type(settings), 'format_price', lambda self, amount: f'{amount / 100:.2f}₽', raising=False)
    service.build_topup_success_keyboard = AsyncMock(return_value=None)

    payload = {
        'update_type': 'invoice_paid',
        'payload': {
            'invoice_id': 'inv_1',
            'paid_at': '2024-01-01T12:00:00Z',
        },
    }

    result = await service.process_cryptobot_webhook(fake_session, payload)

    assert result is True
    assert transactions and transactions[0]['amount_kopeks'] == 14000
    assert user.balance_kopeks == 14000
    assert payment.transaction_id == 888
    assert bot.sent_messages
    assert admin_calls


@pytest.mark.anyio('asyncio')
async def test_process_heleket_webhook_success(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = DummyBot()
    service = _make_service(bot)
    fake_session = FakeSession()

    payment = SimpleNamespace(
        id=5001,
        uuid='heleket-uuid',
        order_id='heleket-order',
        user_id=77,
        amount='150.00',
        amount_float=150.0,
        amount_kopeks=15000,
        status='check',
        payer_amount=None,
        payer_currency=None,
        exchange_rate=None,
        discount_percent=None,
        payment_url=None,
        transaction_id=None,
    )

    async def fake_get_by_uuid(db, uuid):
        return payment if uuid == payment.uuid else None

    async def fake_get_by_order(db, order_id):
        return payment if order_id == payment.order_id else None

    async def fake_update(
        db,
        uuid,
        *,
        status=None,
        payer_amount=None,
        payer_currency=None,
        exchange_rate=None,
        discount_percent=None,
        paid_at=None,
        payment_url=None,
        metadata=None,
    ):
        if status is not None:
            payment.status = status
        if payer_amount is not None:
            payment.payer_amount = payer_amount
        if payer_currency is not None:
            payment.payer_currency = payer_currency
        if exchange_rate is not None:
            payment.exchange_rate = exchange_rate
        if discount_percent is not None:
            payment.discount_percent = discount_percent
        if payment_url is not None:
            payment.payment_url = payment_url
        payment.paid_at = paid_at
        if metadata:
            payment.metadata_json = metadata
        return payment

    async def fake_link(db, uuid, transaction_id):
        payment.transaction_id = transaction_id
        return payment

    async def fake_get_by_id_for_update(db, payment_id):
        return payment if payment_id == payment.id else None

    heleket_module = ModuleType('app.database.crud.heleket')
    heleket_module.get_heleket_payment_by_uuid = fake_get_by_uuid
    heleket_module.get_heleket_payment_by_order_id = fake_get_by_order
    heleket_module.get_heleket_payment_by_id_for_update = fake_get_by_id_for_update
    heleket_module.update_heleket_payment = fake_update
    heleket_module.link_heleket_payment_to_transaction = fake_link
    monkeypatch.setitem(sys.modules, 'app.database.crud.heleket', heleket_module)

    transactions: list[dict[str, Any]] = []

    async def fake_create_transaction(db, **kwargs):
        transactions.append(kwargs)
        return SimpleNamespace(id=321, **kwargs)

    monkeypatch.setattr(payment_service_module, 'create_transaction', fake_create_transaction)

    user = SimpleNamespace(
        id=77,
        telegram_id=7700,
        balance_kopeks=0,
        has_made_first_topup=False,
        promo_group=None,
        subscription=None,
        referred_by_id=None,
        referrer=None,
        language='ru',
    )
    user.get_primary_promo_group = lambda: getattr(user, 'promo_group', None)

    async def fake_get_user(db, user_id):
        return user if user_id == user.id else None

    monkeypatch.setattr(payment_service_module, 'get_user_by_id', fake_get_user)
    monkeypatch.setattr('app.services.payment.heleket.format_referrer_info', lambda u: '')

    monkeypatch.setattr('app.database.crud.user.lock_user_for_update', AsyncMock(side_effect=lambda db, u: u))
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())

    monkeypatch.setattr(type(settings), 'format_price', lambda self, amount: f'{amount / 100:.2f}₽', raising=False)

    referral_stub = SimpleNamespace(process_referral_topup=AsyncMock())
    monkeypatch.setitem(sys.modules, 'app.services.referral_service', referral_stub)

    admin_calls: list[Any] = []

    class DummyAdminService:
        def __init__(self, bot):
            self.bot = bot

        async def send_balance_topup_notification(self, *args, **kwargs):
            admin_calls.append((args, kwargs))

    monkeypatch.setitem(
        sys.modules,
        'app.services.admin_notification_service',
        SimpleNamespace(AdminNotificationService=DummyAdminService),
    )

    service.build_topup_success_keyboard = AsyncMock(return_value=None)

    payload = {
        'uuid': 'heleket-uuid',
        'status': 'paid',
        'payer_amount': '2.50',
        'payer_currency': 'USDT',
        'discount_percent': -5,
        'payer_amount_exchange_rate': '0.0166',
        'paid_at': '2024-01-02T12:00:00Z',
        'url': 'https://pay.example',
    }

    result = await service.process_heleket_webhook(fake_session, payload)

    assert result is True
    assert transactions and transactions[0]['payment_method'] == PaymentMethod.HELEKET
    assert payment.transaction_id == 321
    assert user.balance_kopeks == 15000
    assert user.has_made_first_topup is True
    assert fake_session.commits >= 1
    assert bot.sent_messages
    assert admin_calls
    referral_stub.process_referral_topup.assert_awaited_once()


@pytest.mark.anyio('asyncio')
async def test_process_yookassa_webhook_success(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = DummyBot()
    service = _make_service(bot)
    fake_session = FakeSession()
    payment = SimpleNamespace(
        id=1,
        yookassa_payment_id='yk_123',
        user_id=21,
        amount_kopeks=10000,
        transaction_id=None,
        status='pending',
        is_paid=False,
    )

    async def fake_get_payment(db, payment_id):
        return payment

    async def fake_update(db, payment_id, status, is_paid, is_captured, captured_at, payment_method_type):
        payment.status = status
        payment.is_paid = is_paid
        payment.captured_at = captured_at
        return payment

    async def fake_link(db, payment_id, transaction_id):
        payment.transaction_id = transaction_id

    yk_module = ModuleType('app.database.crud.yookassa')
    yk_module.get_yookassa_payment_by_id = fake_get_payment
    yk_module.update_yookassa_payment_status = fake_update
    yk_module.link_yookassa_payment_to_transaction = fake_link
    monkeypatch.setitem(sys.modules, 'app.database.crud.yookassa', yk_module)

    transactions: list[dict[str, Any]] = []

    async def fake_create_transaction(db, **kwargs):
        transactions.append(kwargs)
        return SimpleNamespace(id=999, **kwargs)

    monkeypatch.setattr(payment_service_module, 'create_transaction', fake_create_transaction)
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())

    user = SimpleNamespace(
        id=21,
        telegram_id=2100,
        balance_kopeks=0,
        has_made_first_topup=False,
        promo_group=None,
        subscription=None,
        referred_by_id=None,
        referrer=None,
    )
    user.get_primary_promo_group = lambda: getattr(user, 'promo_group', None)

    async def fake_get_user(db, user_id):
        return user

    monkeypatch.setattr(payment_service_module, 'get_user_by_id', fake_get_user)
    monkeypatch.setattr(type(settings), 'format_price', lambda self, amount: f'{amount / 100:.2f}₽', raising=False)

    async def fake_lock_user(db, locked_user):
        return locked_user

    monkeypatch.setattr('app.database.crud.user.lock_user_for_update', fake_lock_user)
    # ORM-level selects in the success path: the YooKassaPayment FOR UPDATE lock
    # (scalar_one -> payment) and the eager-load User re-query (scalar_one_or_none -> user).
    fake_session.route_by_entity = {'YooKassaPayment': payment, 'User': user}

    # The user-success notification lazily imports app.cabinet.routes.websocket, which
    # transitively imports process_referral_registration from referral_service; expose it
    # on the stub so that import chain resolves.
    referral_mock = SimpleNamespace(
        process_referral_topup=AsyncMock(),
        process_referral_registration=AsyncMock(),
    )
    monkeypatch.setitem(sys.modules, 'app.services.referral_service', referral_mock)

    admin_calls: list[Any] = []

    class DummyAdminService:
        def __init__(self, bot):
            self.bot = bot

        async def send_balance_topup_notification(self, *args, **kwargs):
            admin_calls.append((args, kwargs))

    monkeypatch.setitem(
        sys.modules,
        'app.services.admin_notification_service',
        SimpleNamespace(AdminNotificationService=DummyAdminService),
    )
    service.build_topup_success_keyboard = AsyncMock(return_value=None)

    payload = {
        'object': {
            'id': 'yk_123',
            'status': 'succeeded',
            'paid': True,
            'payment_method': {'type': 'bank_card'},
        }
    }

    result = await service.process_yookassa_webhook(fake_session, payload)

    assert result is True
    assert transactions and transactions[0]['amount_kopeks'] == 10000
    assert payment.transaction_id == 999
    assert payment.is_paid is True
    assert user.balance_kopeks == 10000
    assert bot.sent_messages
    assert admin_calls


@pytest.mark.anyio('asyncio')
async def test_process_yookassa_webhook_uses_remote_status(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = DummyBot()
    service = _make_service(bot)
    fake_session = FakeSession()
    payment = SimpleNamespace(
        id=1,
        yookassa_payment_id='yk_789',
        user_id=42,
        amount_kopeks=20000,
        transaction_id=None,
        status='pending',
        is_paid=False,
    )

    async def fake_get_payment(db, payment_id):
        return payment

    async def fake_update(db, payment_id, status, is_paid, is_captured, captured_at, payment_method_type):
        payment.status = status
        payment.is_paid = is_paid
        payment.captured_at = captured_at
        payment.payment_method_type = payment_method_type
        return payment

    async def fake_link(db, payment_id, transaction_id):
        payment.transaction_id = transaction_id

    yk_module = ModuleType('app.database.crud.yookassa')
    yk_module.get_yookassa_payment_by_id = fake_get_payment
    yk_module.update_yookassa_payment_status = fake_update
    yk_module.link_yookassa_payment_to_transaction = fake_link
    monkeypatch.setitem(sys.modules, 'app.database.crud.yookassa', yk_module)

    transactions: list[dict[str, Any]] = []

    async def fake_create_transaction(db, **kwargs):
        transactions.append(kwargs)
        return SimpleNamespace(id=555, **kwargs)

    monkeypatch.setattr(payment_service_module, 'create_transaction', fake_create_transaction)

    user = SimpleNamespace(
        id=42,
        telegram_id=4200,
        balance_kopeks=0,
        has_made_first_topup=False,
        promo_group=None,
        subscription=None,
        referred_by_id=None,
        referrer=None,
    )
    user.get_primary_promo_group = lambda: getattr(user, 'promo_group', None)

    async def fake_get_user(db, user_id):
        return user

    monkeypatch.setattr(payment_service_module, 'get_user_by_id', fake_get_user)
    monkeypatch.setattr(type(settings), 'format_price', lambda self, amount: f'{amount / 100:.2f}₽', raising=False)

    async def fake_lock_user(db, locked_user):
        return locked_user

    monkeypatch.setattr('app.database.crud.user.lock_user_for_update', fake_lock_user)
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())
    # YooKassaPayment FOR UPDATE lock (scalar_one) and User eager-load (scalar_one_or_none).
    fake_session.route_by_entity = {'YooKassaPayment': payment, 'User': user}

    referral_mock = SimpleNamespace(
        process_referral_topup=AsyncMock(),
        process_referral_registration=AsyncMock(),
    )
    monkeypatch.setitem(sys.modules, 'app.services.referral_service', referral_mock)

    admin_calls: list[Any] = []

    class DummyAdminService:
        def __init__(self, bot):
            self.bot = bot

        async def send_balance_topup_notification(self, *args, **kwargs):
            admin_calls.append((args, kwargs))

    monkeypatch.setitem(
        sys.modules,
        'app.services.admin_notification_service',
        SimpleNamespace(AdminNotificationService=DummyAdminService),
    )

    service.build_topup_success_keyboard = AsyncMock(return_value=None)

    remote_payload = {
        'id': 'yk_789',
        'status': 'succeeded',
        'paid': True,
        'amount_value': 200.0,
        'amount_currency': 'rub',
        'payment_method_type': 'bank_card',
        'refundable': True,
    }

    get_info_mock = AsyncMock(return_value=remote_payload)
    service.yookassa_service = SimpleNamespace(get_payment_info=get_info_mock)

    payload = {
        'object': {
            'id': 'yk_789',
            'status': 'pending',
            'paid': False,
        }
    }

    result = await service.process_yookassa_webhook(fake_session, payload)

    assert result is True
    assert payment.status == 'succeeded'
    assert payment.is_paid is True
    assert transactions and transactions[0]['amount_kopeks'] == 20000
    assert payment.transaction_id == 555
    get_info_mock.assert_awaited_once_with('yk_789')
    assert admin_calls


@pytest.mark.anyio('asyncio')
async def test_process_yookassa_webhook_handles_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = DummyBot()
    service = _make_service(bot)
    fake_session = FakeSession()
    payment = SimpleNamespace(
        yookassa_payment_id='yk_cancel',
        user_id=77,
        amount_kopeks=5000,
        transaction_id=None,
        status='pending',
        is_paid=False,
        captured_at=None,
        payment_method_type=None,
    )

    async def fake_get_payment(db, payment_id):
        return payment

    monkeypatch.setattr(
        payment_service_module,
        'get_yookassa_payment_by_id',
        fake_get_payment,
    )

    get_info_mock = AsyncMock(
        return_value={
            'id': 'yk_cancel',
            'status': 'canceled',
            'paid': False,
            'amount_value': 50.0,
            'amount_currency': 'RUB',
        }
    )
    service.yookassa_service = SimpleNamespace(get_payment_info=get_info_mock)

    payload = {
        'object': {
            'id': 'yk_cancel',
            'status': 'pending',
            'paid': False,
        }
    }

    result = await service.process_yookassa_webhook(fake_session, payload)

    assert result is True
    assert payment.status == 'canceled'
    assert payment.is_paid is False
    assert fake_session.commits == 1
    assert fake_session.refreshed and fake_session.refreshed[0] is payment
    assert bot.sent_messages == []
    get_info_mock.assert_awaited_once_with('yk_cancel')


@pytest.mark.anyio('asyncio')
async def test_process_yookassa_webhook_restores_missing_payment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = DummyBot()
    service = _make_service(bot)
    fake_session = FakeSession()

    restored_payment = SimpleNamespace(
        id=999,
        yookassa_payment_id='yk_456',
        user_id=21,
        amount_kopeks=0,
        status='pending',
        is_paid=False,
        transaction_id=None,
        description='',
        payment_method_type=None,
        confirmation_url=None,
        metadata_json=None,
        test_mode=False,
        refundable=False,
    )

    get_calls = {'count': 0}

    async def fake_get_payment(db, payment_id):
        get_calls['count'] += 1
        if get_calls['count'] == 1:
            return None
        return restored_payment

    async def fake_create_payment(**kwargs: Any):
        restored_payment.user_id = kwargs['user_id']
        restored_payment.amount_kopeks = kwargs['amount_kopeks']
        restored_payment.status = kwargs['status']
        restored_payment.description = kwargs['description']
        restored_payment.payment_method_type = kwargs['payment_method_type']
        restored_payment.confirmation_url = kwargs['confirmation_url']
        restored_payment.metadata_json = kwargs['metadata_json']
        restored_payment.test_mode = kwargs['test_mode']
        restored_payment.yookassa_payment_id = kwargs['yookassa_payment_id']
        restored_payment.yookassa_created_at = kwargs['yookassa_created_at']
        return restored_payment

    async def fake_update_status(
        db,
        yookassa_payment_id,
        status,
        is_paid,
        is_captured,
        captured_at,
        payment_method_type,
    ):
        restored_payment.status = status
        restored_payment.is_paid = is_paid
        restored_payment.is_captured = is_captured
        restored_payment.captured_at = captured_at
        restored_payment.payment_method_type = payment_method_type
        return restored_payment

    async def fake_link(db, yookassa_payment_id, transaction_id):
        restored_payment.transaction_id = transaction_id

    monkeypatch.setattr(payment_service_module, 'get_yookassa_payment_by_id', fake_get_payment)
    monkeypatch.setattr(payment_service_module, 'create_yookassa_payment', fake_create_payment)
    monkeypatch.setattr(payment_service_module, 'update_yookassa_payment_status', fake_update_status)
    monkeypatch.setattr(payment_service_module, 'link_yookassa_payment_to_transaction', fake_link)

    transactions: list[dict[str, Any]] = []

    async def fake_create_transaction(db, **kwargs):
        transactions.append(kwargs)
        return SimpleNamespace(id=555, **kwargs)

    monkeypatch.setattr(payment_service_module, 'create_transaction', fake_create_transaction)

    user = SimpleNamespace(
        id=21,
        telegram_id=2100,
        balance_kopeks=0,
        has_made_first_topup=False,
        promo_group=None,
        subscription=None,
        referred_by_id=None,
        referrer=None,
    )
    user.get_primary_promo_group = lambda: getattr(user, 'promo_group', None)

    async def fake_get_user(db, user_id):
        return user

    monkeypatch.setattr(payment_service_module, 'get_user_by_id', fake_get_user)
    monkeypatch.setattr(type(settings), 'format_price', lambda self, amount: f'{amount / 100:.2f}₽', raising=False)

    # The restore path resolves the user via direct `from app.database.crud.user import ...`,
    # so patch those helpers (lookup + row lock) on the real module without replacing it
    # (other code imports many other symbols from crud.user).
    async def fake_get_by_tg(db, tg):
        return user

    async def fake_lock_user(db, locked_user):
        return locked_user

    monkeypatch.setattr('app.database.crud.user.get_user_by_id', fake_get_user)
    monkeypatch.setattr('app.database.crud.user.get_user_by_telegram_id', fake_get_by_tg)
    monkeypatch.setattr('app.database.crud.user.lock_user_for_update', fake_lock_user)
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())
    # FOR UPDATE re-query of the restored payment (scalar_one) and User eager-load.
    fake_session.route_by_entity = {'YooKassaPayment': restored_payment, 'User': user}

    referral_mock = SimpleNamespace(
        process_referral_topup=AsyncMock(),
        process_referral_registration=AsyncMock(),
    )
    monkeypatch.setitem(sys.modules, 'app.services.referral_service', referral_mock)

    admin_calls: list[Any] = []

    class DummyAdminService:
        def __init__(self, bot):
            self.bot = bot

        async def send_balance_topup_notification(self, *args, **kwargs):
            admin_calls.append((args, kwargs))

    monkeypatch.setitem(
        sys.modules,
        'app.services.admin_notification_service',
        SimpleNamespace(AdminNotificationService=DummyAdminService),
    )
    service.build_topup_success_keyboard = AsyncMock(return_value=None)

    payload = {
        'object': {
            'id': 'yk_456',
            'status': 'succeeded',
            'paid': True,
            'amount': {'value': '150.00', 'currency': 'RUB'},
            'metadata': {'user_id': '21', 'payment_purpose': 'balance_topup'},
            'description': 'Пополнение',
            'payment_method': {'type': 'bank_card'},
            'created_at': '2024-01-02T12:00:00Z',
            'captured_at': '2024-01-02T12:05:00Z',
            'confirmation': {'confirmation_url': 'https://pay.example'},
        }
    }

    result = await service.process_yookassa_webhook(fake_session, payload)

    assert result is True
    assert get_calls['count'] >= 2  # повторный запрос после восстановления
    assert restored_payment.amount_kopeks == 15000
    assert restored_payment.is_paid is True
    assert transactions and transactions[0]['amount_kopeks'] == 15000
    assert restored_payment.transaction_id == 555
    assert user.balance_kopeks == 15000
    assert bot.sent_messages
    assert admin_calls


@pytest.mark.anyio('asyncio')
async def test_process_yookassa_webhook_missing_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service(DummyBot())
    db = FakeSession()

    async def fake_get_payment(db_session, payment_id):
        return None

    create_mock = AsyncMock()
    update_mock = AsyncMock()

    monkeypatch.setattr(payment_service_module, 'get_yookassa_payment_by_id', fake_get_payment)
    monkeypatch.setattr(payment_service_module, 'create_yookassa_payment', create_mock)
    monkeypatch.setattr(payment_service_module, 'update_yookassa_payment_status', update_mock)

    payload = {'object': {'id': 'yk_missing', 'status': 'succeeded', 'paid': True}}

    result = await service.process_yookassa_webhook(db, payload)

    assert result is False
    create_mock.assert_not_awaited()
    update_mock.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_process_yookassa_webhook_missing_id(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = DummyBot()
    service = _make_service(bot)
    db = FakeSession()

    result = await service.process_yookassa_webhook(db, {'object': {}})
    assert result is False


@pytest.mark.anyio('asyncio')
async def test_process_yookassa_webhook_skip_ip_rejects_unconfirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-closed: с YOOKASSA_SKIP_IP_CHECK и без подтверждения от API YooKassa
    (id не найден → get_payment_info вернул None) вебхук должен быть отклонён,
    а восстановление/начисление из тела запроса — не выполнено. Это закрывает
    подделку payment.succeeded при снятом IP-барьере."""
    monkeypatch.setattr(settings, 'YOOKASSA_SKIP_IP_CHECK', True, raising=False)
    service = _make_service(DummyBot())
    db = FakeSession()

    get_payment_mock = AsyncMock()
    create_mock = AsyncMock()
    monkeypatch.setattr(payment_service_module, 'get_yookassa_payment_by_id', get_payment_mock)
    monkeypatch.setattr(payment_service_module, 'create_yookassa_payment', create_mock)

    get_info_mock = AsyncMock(return_value=None)
    service.yookassa_service = SimpleNamespace(get_payment_info=get_info_mock)

    payload = {
        'object': {
            'id': 'yk_forged',
            'status': 'succeeded',
            'paid': True,
            'amount': {'value': '9999.00', 'currency': 'RUB'},
            'metadata': {'user_id': '21'},
        }
    }

    result = await service.process_yookassa_webhook(db, payload)

    assert result is False
    get_info_mock.assert_awaited_once_with('yk_forged')
    # Обработка прерывается до поиска/создания локального платежа.
    get_payment_mock.assert_not_awaited()
    create_mock.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_process_yookassa_webhook_skip_ip_rejects_on_api_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-closed при таймауте API в skip-режиме: нет подтверждения — нет начисления."""
    monkeypatch.setattr(settings, 'YOOKASSA_SKIP_IP_CHECK', True, raising=False)
    service = _make_service(DummyBot())
    db = FakeSession()

    get_payment_mock = AsyncMock()
    create_mock = AsyncMock()
    monkeypatch.setattr(payment_service_module, 'get_yookassa_payment_by_id', get_payment_mock)
    monkeypatch.setattr(payment_service_module, 'create_yookassa_payment', create_mock)

    get_info_mock = AsyncMock(side_effect=TimeoutError())
    service.yookassa_service = SimpleNamespace(get_payment_info=get_info_mock)

    payload = {'object': {'id': 'yk_slow', 'status': 'succeeded', 'paid': True}}

    result = await service.process_yookassa_webhook(db, payload)

    assert result is False
    get_info_mock.assert_awaited_once_with('yk_slow')
    get_payment_mock.assert_not_awaited()
    create_mock.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_process_yookassa_webhook_skip_ip_credits_when_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """В skip-режиме подтверждённый API платёж начисляется как обычно;
    финансовые поля берутся из ответа API, а не из тела вебхука."""
    monkeypatch.setattr(settings, 'YOOKASSA_SKIP_IP_CHECK', True, raising=False)
    bot = DummyBot()
    service = _make_service(bot)
    fake_session = FakeSession()
    payment = SimpleNamespace(
        id=1,
        yookassa_payment_id='yk_ok',
        user_id=21,
        amount_kopeks=10000,
        transaction_id=None,
        status='pending',
        is_paid=False,
    )

    async def fake_get_payment(db, payment_id):
        return payment

    async def fake_update(db, payment_id, status, is_paid, is_captured, captured_at, payment_method_type):
        payment.status = status
        payment.is_paid = is_paid
        payment.captured_at = captured_at
        return payment

    async def fake_link(db, payment_id, transaction_id):
        payment.transaction_id = transaction_id

    yk_module = ModuleType('app.database.crud.yookassa')
    yk_module.get_yookassa_payment_by_id = fake_get_payment
    yk_module.update_yookassa_payment_status = fake_update
    yk_module.link_yookassa_payment_to_transaction = fake_link
    monkeypatch.setitem(sys.modules, 'app.database.crud.yookassa', yk_module)

    transactions: list[dict[str, Any]] = []

    async def fake_create_transaction(db, **kwargs):
        transactions.append(kwargs)
        return SimpleNamespace(id=999, **kwargs)

    monkeypatch.setattr(payment_service_module, 'create_transaction', fake_create_transaction)
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())

    user = SimpleNamespace(
        id=21,
        telegram_id=2100,
        balance_kopeks=0,
        has_made_first_topup=False,
        promo_group=None,
        subscription=None,
        referred_by_id=None,
        referrer=None,
    )
    user.get_primary_promo_group = lambda: getattr(user, 'promo_group', None)

    async def fake_get_user(db, user_id):
        return user

    monkeypatch.setattr(payment_service_module, 'get_user_by_id', fake_get_user)
    monkeypatch.setattr(type(settings), 'format_price', lambda self, amount: f'{amount / 100:.2f}₽', raising=False)

    async def fake_lock_user(db, locked_user):
        return locked_user

    monkeypatch.setattr('app.database.crud.user.lock_user_for_update', fake_lock_user)
    fake_session.route_by_entity = {'YooKassaPayment': payment, 'User': user}

    referral_mock = SimpleNamespace(
        process_referral_topup=AsyncMock(),
        process_referral_registration=AsyncMock(),
    )
    monkeypatch.setitem(sys.modules, 'app.services.referral_service', referral_mock)

    admin_calls: list[Any] = []

    class DummyAdminService:
        def __init__(self, bot):
            self.bot = bot

        async def send_balance_topup_notification(self, *args, **kwargs):
            admin_calls.append((args, kwargs))

    monkeypatch.setitem(
        sys.modules,
        'app.services.admin_notification_service',
        SimpleNamespace(AdminNotificationService=DummyAdminService),
    )
    service.build_topup_success_keyboard = AsyncMock(return_value=None)

    get_info_mock = AsyncMock(
        return_value={
            'id': 'yk_ok',
            'status': 'succeeded',
            'paid': True,
            'amount_value': 100.0,
            'amount_currency': 'RUB',
            'payment_method_type': 'bank_card',
            'refundable': True,
        }
    )
    service.yookassa_service = SimpleNamespace(get_payment_info=get_info_mock)

    # Тело намеренно "непроплаченное" — начисление должно опереться на ответ API.
    payload = {'object': {'id': 'yk_ok', 'status': 'pending', 'paid': False}}

    result = await service.process_yookassa_webhook(fake_session, payload)

    assert result is True
    assert payment.status == 'succeeded'
    assert payment.is_paid is True
    assert transactions and transactions[0]['amount_kopeks'] == 10000
    assert payment.transaction_id == 999
    assert user.balance_kopeks == 10000
    get_info_mock.assert_awaited_once_with('yk_ok')
    assert admin_calls


@pytest.mark.anyio('asyncio')
async def test_process_yookassa_webhook_default_mode_failopen_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Регрессия: при выключенном флаге (дефолт) отсутствие подтверждения от API
    НЕ блокирует обработку — сохраняется исторический fail-open по данным вебхука,
    ради которого откатывали #1786 (иначе деградация API вешала бы вебхуки)."""
    monkeypatch.setattr(settings, 'YOOKASSA_SKIP_IP_CHECK', False, raising=False)
    bot = DummyBot()
    service = _make_service(bot)
    fake_session = FakeSession()
    payment = SimpleNamespace(
        id=1,
        yookassa_payment_id='yk_fo',
        user_id=21,
        amount_kopeks=10000,
        transaction_id=None,
        status='pending',
        is_paid=False,
    )

    async def fake_get_payment(db, payment_id):
        return payment

    monkeypatch.setattr(payment_service_module, 'get_yookassa_payment_by_id', fake_get_payment)

    transactions: list[dict[str, Any]] = []

    async def fake_create_transaction(db, **kwargs):
        transactions.append(kwargs)
        return SimpleNamespace(id=999, **kwargs)

    monkeypatch.setattr(payment_service_module, 'create_transaction', fake_create_transaction)
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())

    user = SimpleNamespace(
        id=21,
        telegram_id=2100,
        balance_kopeks=0,
        has_made_first_topup=False,
        promo_group=None,
        subscription=None,
        referred_by_id=None,
        referrer=None,
    )
    user.get_primary_promo_group = lambda: getattr(user, 'promo_group', None)

    async def fake_get_user(db, user_id):
        return user

    monkeypatch.setattr(payment_service_module, 'get_user_by_id', fake_get_user)
    monkeypatch.setattr(type(settings), 'format_price', lambda self, amount: f'{amount / 100:.2f}₽', raising=False)

    async def fake_lock_user(db, locked_user):
        return locked_user

    monkeypatch.setattr('app.database.crud.user.lock_user_for_update', fake_lock_user)
    fake_session.route_by_entity = {'YooKassaPayment': payment, 'User': user}

    referral_mock = SimpleNamespace(
        process_referral_topup=AsyncMock(),
        process_referral_registration=AsyncMock(),
    )
    monkeypatch.setitem(sys.modules, 'app.services.referral_service', referral_mock)

    admin_calls: list[Any] = []

    class DummyAdminService:
        def __init__(self, bot):
            self.bot = bot

        async def send_balance_topup_notification(self, *args, **kwargs):
            admin_calls.append((args, kwargs))

    monkeypatch.setitem(
        sys.modules,
        'app.services.admin_notification_service',
        SimpleNamespace(AdminNotificationService=DummyAdminService),
    )
    service.build_topup_success_keyboard = AsyncMock(return_value=None)

    # API не подтверждает (None), но флаг выключен → доверяем телу вебхука.
    get_info_mock = AsyncMock(return_value=None)
    service.yookassa_service = SimpleNamespace(get_payment_info=get_info_mock)

    payload = {
        'object': {
            'id': 'yk_fo',
            'status': 'succeeded',
            'paid': True,
            'payment_method': {'type': 'bank_card'},
        }
    }

    result = await service.process_yookassa_webhook(fake_session, payload)

    assert result is True
    assert transactions and transactions[0]['amount_kopeks'] == 10000
    assert user.balance_kopeks == 10000
    get_info_mock.assert_awaited_once_with('yk_fo')


@pytest.mark.anyio('asyncio')
async def test_process_pal24_callback_success(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = DummyBot()
    service = _make_service(bot)
    service.pal24_service = SimpleNamespace(is_configured=True)
    fake_session = FakeSession()
    payment = SimpleNamespace(
        id=1,
        bill_id='BILL-1',
        order_id='order-1',
        amount_kopeks=5000,
        user_id=33,
        transaction_id=None,
        is_paid=False,
        status='NEW',
        metadata_json={},
        payment_method=None,
        paid_at=None,
    )

    async def fake_get_by_order(db, order_id):
        return payment

    async def fake_get_by_bill(db, bill_id):
        return payment

    async def fake_get_for_update(db, payment_id):
        return payment

    async def fake_update(db, payment_obj, **kwargs):
        payment.status = kwargs.get('status', payment.status)
        payment.is_paid = kwargs.get('is_paid', payment.is_paid)
        payment.payment_status = kwargs.get('payment_status', payment.status)
        payment.callback_payload = kwargs.get('callback_payload')
        return payment

    async def fake_link(db, payment_obj, transaction_id):
        payment.transaction_id = transaction_id

    pal_module = ModuleType('app.database.crud.pal24')
    pal_module.get_pal24_payment_by_order_id = fake_get_by_order
    pal_module.get_pal24_payment_by_bill_id = fake_get_by_bill
    pal_module.get_pal24_payment_by_id_for_update = fake_get_for_update
    pal_module.update_pal24_payment_status = fake_update
    pal_module.link_pal24_payment_to_transaction = fake_link
    monkeypatch.setitem(sys.modules, 'app.database.crud.pal24', pal_module)
    monkeypatch.setattr(payment_service_module, 'get_pal24_payment_by_order_id', fake_get_by_order)
    monkeypatch.setattr(payment_service_module, 'get_pal24_payment_by_bill_id', fake_get_by_bill)
    monkeypatch.setattr(payment_service_module, 'update_pal24_payment_status', fake_update)
    monkeypatch.setattr(payment_service_module, 'link_pal24_payment_to_transaction', fake_link)

    async def fake_create_transaction(db, **kwargs):
        payment.transaction_id = 654
        return SimpleNamespace(id=654, **kwargs)

    monkeypatch.setattr(payment_service_module, 'create_transaction', fake_create_transaction)

    user = SimpleNamespace(
        id=33,
        telegram_id=3300,
        balance_kopeks=0,
        has_made_first_topup=False,
        promo_group=None,
        subscription=None,
        referred_by_id=None,
        referrer=None,
        language='ru',
    )
    user.get_primary_promo_group = lambda: getattr(user, 'promo_group', None)

    async def fake_get_user(db, user_id):
        return user

    monkeypatch.setattr(payment_service_module, 'get_user_by_id', fake_get_user)
    monkeypatch.setattr(type(settings), 'format_price', lambda self, amount: f'{amount / 100:.2f}₽', raising=False)

    monkeypatch.setattr('app.database.crud.user.lock_user_for_update', AsyncMock(return_value=user))
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())

    referral_pal = SimpleNamespace(
        process_referral_topup=AsyncMock(),
        process_referral_registration=AsyncMock(),
    )
    monkeypatch.setitem(sys.modules, 'app.services.referral_service', referral_pal)

    admin_calls: list[Any] = []

    class DummyAdminServicePal:
        def __init__(self, bot):
            self.bot = bot

        async def send_balance_topup_notification(self, *args, **kwargs):
            admin_calls.append((args, kwargs))

    monkeypatch.setitem(
        sys.modules,
        'app.services.admin_notification_service',
        SimpleNamespace(AdminNotificationService=DummyAdminServicePal),
    )

    service.build_topup_success_keyboard = AsyncMock(return_value=None)

    payload = {
        'InvId': 'order-1',
        'OutSum': '50.00',
        'Status': 'SUCCESS',
        'TrsId': 'trs-1',
    }

    result = await service.process_pal24_callback(fake_session, payload)

    assert result is True
    assert payment.transaction_id == 654
    assert user.balance_kopeks == 5000
    assert admin_calls
    # The success path now sends a single "Пополнение успешно!" message to the user;
    # the separate saved-cart message (return_to_saved_cart) was removed when the
    # duplicate post-topup message was dropped from production.
    assert bot.sent_messages
    success_message = bot.sent_messages[-1]
    assert success_message['args'][0] == user.telegram_id
    assert 'Пополнение успешно' in success_message['args'][1]


@pytest.mark.anyio('asyncio')
async def test_get_pal24_payment_status_auto_finalize(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = DummyBot()
    service = _make_service(bot)

    class DummyPal24Service:
        BILL_SUCCESS_STATES = {'SUCCESS', 'OVERPAID'}
        BILL_FAILED_STATES = {'FAIL'}
        BILL_PENDING_STATES = {'NEW', 'PROCESS', 'UNDERPAID'}

        async def get_bill_status(self, bill_id: str) -> dict[str, Any]:
            return {
                'status': 'SUCCESS',
                'bill': {
                    'status': 'SUCCESS',
                    'payments': [
                        {
                            'id': 'trs-auto-1',
                            'status': 'SUCCESS',
                            'method': 'SBP',
                            'balance_amount': '50.00',
                            'balance_currency': 'RUB',
                        }
                    ],
                },
            }

        async def get_payment_status(self, payment_id: str) -> dict[str, Any] | None:
            return None

        async def get_bill_payments(self, bill_id: str) -> dict[str, Any] | None:
            return {
                'data': [
                    {
                        'id': 'trs-auto-1',
                        'bill_id': bill_id,
                        'status': 'SUCCESS',
                        'payment_method': 'SBP',
                    }
                ]
            }

    service.pal24_service = DummyPal24Service()

    fake_session = FakeSession()
    payment = SimpleNamespace(
        id=77,
        bill_id='BILL-AUTO',
        order_id='order-auto',
        amount_kopeks=5000,
        user_id=91,
        transaction_id=None,
        is_paid=False,
        status='NEW',
        metadata_json={},
        payment_id=None,
        payment_method=None,
        paid_at=None,
    )

    async def fake_get_payment_by_id(db, local_id):
        return payment

    async def fake_update_payment(db, payment_obj, **kwargs):
        for key, value in kwargs.items():
            setattr(payment, key, value)
        return payment

    async def fake_link_payment(db, payment_obj, transaction_id):
        payment.transaction_id = transaction_id
        return payment

    monkeypatch.setattr(payment_service_module, 'get_pal24_payment_by_id', fake_get_payment_by_id)
    monkeypatch.setattr(payment_service_module, 'update_pal24_payment_status', fake_update_payment)
    monkeypatch.setattr(payment_service_module, 'link_pal24_payment_to_transaction', fake_link_payment)

    # Auto-finalize acquires a FOR UPDATE lock via import_module('app.database.crud.pal24')
    # and locks the user row before crediting; stub both so the real DB calls are bypassed.
    async def fake_pal24_lock(db, pid):
        return payment

    async def fake_lock_user(db, locked_user):
        return locked_user

    monkeypatch.setattr('app.database.crud.pal24.get_pal24_payment_by_id_for_update', fake_pal24_lock)
    monkeypatch.setattr('app.database.crud.user.lock_user_for_update', fake_lock_user)
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())

    transactions: list[dict[str, Any]] = []

    async def fake_create_transaction(db, **kwargs):
        transactions.append(kwargs)
        payment.transaction_id = 999
        return SimpleNamespace(id=999, **kwargs)

    monkeypatch.setattr(payment_service_module, 'create_transaction', fake_create_transaction)

    user = SimpleNamespace(
        id=91,
        telegram_id=9100,
        balance_kopeks=0,
        has_made_first_topup=False,
        promo_group=None,
        subscription=None,
        referred_by_id=None,
        referrer=None,
        language='ru',
    )
    user.get_primary_promo_group = lambda: getattr(user, 'promo_group', None)

    async def fake_get_user(db, user_id):
        return user

    monkeypatch.setattr(payment_service_module, 'get_user_by_id', fake_get_user)
    monkeypatch.setattr(type(settings), 'format_price', lambda self, amount: f'{amount / 100:.2f}₽', raising=False)

    referral_stub = SimpleNamespace(
        process_referral_topup=AsyncMock(),
        process_referral_registration=AsyncMock(),
    )
    monkeypatch.setitem(sys.modules, 'app.services.referral_service', referral_stub)

    admin_notifications: list[Any] = []

    class DummyAdminService:
        def __init__(self, bot):
            self.bot = bot

        async def send_balance_topup_notification(self, *args, **kwargs):
            admin_notifications.append((args, kwargs))

    monkeypatch.setitem(
        sys.modules,
        'app.services.admin_notification_service',
        SimpleNamespace(AdminNotificationService=DummyAdminService),
    )

    user_cart_stub = SimpleNamespace(user_cart_service=SimpleNamespace(has_user_cart=AsyncMock(return_value=False)))
    monkeypatch.setitem(sys.modules, 'app.services.user_cart_service', user_cart_stub)

    class DummyTypes:
        class InlineKeyboardMarkup:
            def __init__(self, inline_keyboard=None, **kwargs):
                self.inline_keyboard = inline_keyboard or []
                self.kwargs = kwargs

        class InlineKeyboardButton:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

    monkeypatch.setitem(sys.modules, 'aiogram', SimpleNamespace(types=DummyTypes))

    service.build_topup_success_keyboard = AsyncMock(return_value=None)

    result = await service.get_pal24_payment_status(fake_session, payment.id)

    assert result is not None
    assert payment.transaction_id == 999
    assert user.balance_kopeks == 5000
    assert bot.sent_messages
    assert admin_notifications
    assert transactions and transactions[0]['user_id'] == 91


@pytest.mark.anyio('asyncio')
async def test_process_pal24_callback_payment_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = DummyBot()
    service = _make_service(bot)
    service.pal24_service = SimpleNamespace(is_configured=True)
    db = FakeSession()

    async def fake_get_by_order(db, order_id):
        return None

    async def fake_get_by_bill(db, bill_id):
        return None

    pal_module = ModuleType('app.database.crud.pal24')
    pal_module.get_pal24_payment_by_order_id = fake_get_by_order
    pal_module.get_pal24_payment_by_bill_id = fake_get_by_bill
    pal_module.update_pal24_payment_status = AsyncMock()
    pal_module.link_pal24_payment_to_transaction = AsyncMock()
    monkeypatch.setitem(sys.modules, 'app.database.crud.pal24', pal_module)
    monkeypatch.setattr(payment_service_module, 'get_pal24_payment_by_order_id', fake_get_by_order)
    monkeypatch.setattr(payment_service_module, 'get_pal24_payment_by_bill_id', fake_get_by_bill)

    payload = {
        'InvId': 'order-unknown',
        'OutSum': '10.00',
        'Status': 'SUCCESS',
    }

    result = await service.process_pal24_callback(db, payload)
    assert result is False
