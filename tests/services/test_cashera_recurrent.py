"""Автопродление через подписки Cashera: оформление, события подписки, списания.

Сквозь SQLite со всеми таблицами — продление, транзакции и идемпотентность
проверяются на настоящих строках, а не на моках CRUD.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

import app.services.payment.cashera as cashera_module
from app.config import settings
from app.database.models import (
    Base,
    CasheraSubscription,
    PaymentMethod,
    Subscription,
    SubscriptionStatus,
    Tariff,
    Transaction,
    User,
    UserStatus,
)
from app.services import cashera_recurrent as cr
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)
PRICE = 29950  # 299,50 ₽ — Cashera примет только целые рубли


class StubCashera:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.charges: list[dict[str, Any]] = []

    async def create_subscription(self, **kwargs: Any) -> dict[str, Any]:
        self.created.append(kwargs)
        return {
            'uuid': f'sub-{len(self.created)}',
            'status': 'pending_agreement',
            'payment_url': 'pay.cashera.cash/sub',
        }

    async def cancel_subscription(self, uuid: str) -> dict[str, Any]:
        self.cancelled.append(uuid)
        return {'status': 'cancelled'}

    async def list_subscription_charges(self, _uuid: str, **_kw: Any) -> list[dict[str, Any]]:
        return self.charges


@pytest.fixture
def stub(monkeypatch) -> StubCashera:
    for key, value in {
        'CASHERA_ENABLED': True,
        'CASHERA_API_KEY': 'pk_test',
        'CASHERA_API_SECRET': 'sk_test',
        'CASHERA_RECURRENT_ENABLED': True,
        'WEBHOOK_URL': 'https://bot.example.com',
    }.items():
        monkeypatch.setattr(settings, key, value, raising=False)
    stub = StubCashera()
    monkeypatch.setattr(cashera_module, 'cashera_service', stub)
    monkeypatch.setattr('app.services.cashera_recurring_cancel.cashera_service', stub)
    # Панель и внешние провайдеры в этих тестах не проверяются.
    monkeypatch.setattr(
        'app.services.subscription_service.SubscriptionService.update_remnawave_user', AsyncMock(return_value=None)
    )
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', AsyncMock())
    monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', AsyncMock())
    return stub


async def _seed(db, *, is_trial: bool = False, autopay: bool = True):
    now = datetime.now(UTC)
    user = User(telegram_id=777, username='u', status=UserStatus.ACTIVE.value, language='ru', balance_kopeks=0)
    db.add(user)
    await db.commit()
    tariff = Tariff(name='Pro', is_active=True, device_limit=1, traffic_limit_gb=0, period_prices={'30': PRICE})
    db.add(tariff)
    await db.commit()
    subscription = Subscription(
        user_id=user.id,
        tariff_id=tariff.id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=is_trial,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=5),
        device_limit=1,
        autopay_enabled=autopay,
        remnawave_short_id='shortcas',
    )
    db.add(subscription)
    await db.commit()
    return user.id, tariff, subscription


def _charge(uuid: str, *, status: str = 'paid', amount: int = 30000) -> dict[str, Any]:
    return {
        'uuid': uuid,
        'status': status,
        'amount': amount,
        'currency': 'RUB',
        'payment_method': 'sbp_recurring',
        'paid_at': datetime.now(UTC).isoformat(),
        'external_id': f'recurring.sub-1.{uuid}',
    }


def _charge_event(uuid: str, **kw: Any) -> dict[str, Any]:
    return {
        'event': 'transaction.status_updated',
        'transaction': _charge(uuid, **kw),
        'subscription': {'uuid': 'sub-1', 'external_id': 'ignored'},
    }


async def _enable(db, stub, tariff, subscription, user_id):
    return await cashera_module._CasheraRecurrentAgent().create_cashera_recurrent_subscription(
        db, user_id=user_id, subscription=subscription, tariff=tariff
    )


async def _end_date(db, subscription_id: int) -> datetime:
    await db.rollback()
    value = (await db.execute(select(Subscription.end_date).where(Subscription.id == subscription_id))).scalar_one()
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _record(db) -> CasheraSubscription:
    await db.rollback()
    return (await db.execute(select(CasheraSubscription))).scalar_one()


# --- чистые правила ------------------------------------------------------------------


@pytest.mark.parametrize(
    ('period', 'daily', 'expected'),
    [
        (30, False, ('monthly', 30)),
        (7, False, ('weekly', 7)),
        (365, False, ('yearly', 365)),
        (1, True, ('daily', 1)),
        (90, False, ('monthly', 30)),
    ],
)
def test_interval_mapping(period, daily, expected):
    assert cr.resolve_cashera_interval(period, daily) == expected


def test_amount_rounds_up_to_whole_rubles():
    assert cr.round_up_to_rubles(29950) == 30000
    assert cr.round_up_to_rubles(30000) == 30000
    assert cr.round_up_to_rubles(0) == 0


@pytest.mark.parametrize(
    ('local', 'remote', 'age', 'missing', 'expected'),
    [
        ('PENDING', 'active', 5, False, 'ACTIVE'),
        ('ACTIVE', 'cancelled', 5, False, 'CANCELLED'),
        ('PENDING', 'pending_agreement', 60, False, None),
        ('PENDING', 'pending_agreement', 25 * 60, False, 'FAILED'),
        ('PENDING', None, 45, True, 'FAILED'),
        ('PENDING', None, 45, False, None),  # транспортный сбой — хоронить рано
        ('CANCELLED', 'failed', 5, False, None),
    ],
)
def test_reconcile_decision(local, remote, age, missing, expected):
    assert cr.cashera_reconcile_decision(local, remote, age, remote_missing=missing) == expected


# --- оформление ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_creates_binding_with_rounded_amount_and_disables_balance_autopay(monkeypatch, stub):
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, tariff, subscription = await _seed(db)
        result = await _enable(db, stub, tariff, subscription, user_id)

        record = await _record(db)
        sub = await db.get(Subscription, subscription.id)

    assert stub.created[0]['interval'] == 'monthly'
    assert stub.created[0]['amount_kopeks'] == 30000
    assert stub.created[0]['callback_url'] == 'https://bot.example.com/cashera-webhook'
    assert record.status == 'PENDING'
    assert record.cashera_subscription_uuid == 'sub-1'
    assert result['redirect_url'] == 'https://pay.cashera.cash/sub'
    assert sub.autopay_enabled is False


@pytest.mark.asyncio
async def test_enable_is_idempotent_for_live_binding(monkeypatch, stub):
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, tariff, subscription = await _seed(db)
        await _enable(db, stub, tariff, subscription, user_id)
        await _enable(db, stub, tariff, subscription, user_id)

    assert len(stub.created) == 1


@pytest.mark.asyncio
async def test_enable_refuses_trial_before_calling_cashera(monkeypatch, stub):
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, tariff, subscription = await _seed(db, is_trial=True)
        with pytest.raises(ValueError):
            await cashera_module.enable_cashera_recurring(db, user_id=user_id, subscription=subscription, tariff=tariff)
    assert stub.created == []


# --- события подписки ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscription_activation_event(monkeypatch, stub):
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, tariff, subscription = await _seed(db)
        await _enable(db, stub, tariff, subscription, user_id)
        payload = {'event': 'subscription.status_updated', 'subscription': {'uuid': 'sub-1', 'status': 'active'}}

        assert await cashera_module._CasheraRecurrentAgent().process_cashera_webhook(db, payload) is True
        record = await _record(db)

    assert record.status == 'ACTIVE'
    assert record.remote_status == 'active'


@pytest.mark.asyncio
async def test_locally_cancelled_binding_alive_at_cashera_is_cancelled_again(monkeypatch, stub):
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, tariff, subscription = await _seed(db)
        result = await _enable(db, stub, tariff, subscription, user_id)
        await cashera_module._CasheraRecurrentAgent().cancel_cashera_recurrent_subscription(
            db, local_id=result['local_id']
        )
        stub.cancelled.clear()

        payload = {'event': 'subscription.status_updated', 'subscription': {'uuid': 'sub-1', 'status': 'active'}}
        await cashera_module._CasheraRecurrentAgent().process_cashera_webhook(db, payload)
        record = await _record(db)

    assert record.status == 'CANCELLED'
    assert stub.cancelled == ['sub-1']


# --- списания ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paid_charge_extends_once_and_writes_transaction(monkeypatch, stub):
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, tariff, subscription = await _seed(db)
        sub_id = subscription.id  # после rollback объект экспирируется
        await _enable(db, stub, tariff, subscription, user_id)
        before = await _end_date(db, sub_id)
        agent = cashera_module._CasheraRecurrentAgent()

        assert await agent.process_cashera_webhook(db, _charge_event('ch-1')) is True
        after = await _end_date(db, sub_id)
        assert after - before == timedelta(days=30)

        # Повтор того же списания — второй раз не продлеваем.
        await agent.process_cashera_webhook(db, _charge_event('ch-1'))
        assert await _end_date(db, sub_id) == after

        record = await _record(db)
        payments = (
            await db.execute(
                select(func.count())
                .select_from(Transaction)
                .where(Transaction.payment_method == PaymentMethod.CASHERA.value, Transaction.external_id == 'ch-1')
            )
        ).scalar_one()

    assert record.status == 'ACTIVE'
    assert record.charges_success == 1
    assert record.next_charge_at is not None
    assert payments == 1


@pytest.mark.asyncio
async def test_failed_charge_marks_past_due_but_keeps_cancelled(monkeypatch, stub):
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, tariff, subscription = await _seed(db)
        result = await _enable(db, stub, tariff, subscription, user_id)
        agent = cashera_module._CasheraRecurrentAgent()

        await agent.process_cashera_webhook(db, _charge_event('ch-f', status='failed'))
        assert (await _record(db)).status == 'PAST_DUE'

        await agent.cancel_cashera_recurrent_subscription(db, local_id=result['local_id'])
        await agent.process_cashera_webhook(db, _charge_event('ch-f2', status='failed'))
        assert (await _record(db)).status == 'CANCELLED'


@pytest.mark.asyncio
async def test_charge_on_locally_cancelled_binding_extends_but_does_not_resurrect(monkeypatch, stub):
    """Деньги взяты — продлеваем; запись не воскрешаем и повторяем удалённую отмену."""
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, tariff, subscription = await _seed(db)
        result = await _enable(db, stub, tariff, subscription, user_id)
        agent = cashera_module._CasheraRecurrentAgent()
        await agent.cancel_cashera_recurrent_subscription(db, local_id=result['local_id'])
        stub.cancelled.clear()
        before = await _end_date(db, subscription.id)

        await agent.process_cashera_webhook(db, _charge_event('ch-late'))

        assert await _end_date(db, subscription.id) - before == timedelta(days=30)
        assert (await _record(db)).status == 'CANCELLED'
    assert stub.cancelled == ['sub-1']


@pytest.mark.asyncio
async def test_missed_charges_are_replayed_from_history(monkeypatch, stub):
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, tariff, subscription = await _seed(db)
        result = await _enable(db, stub, tariff, subscription, user_id)
        agent = cashera_module._CasheraRecurrentAgent()
        await agent.process_cashera_webhook(db, _charge_event('ch-1'))
        before = await _end_date(db, subscription.id)

        stub.charges = [_charge('ch-1'), _charge('ch-2'), _charge('ch-x', status='failed')]
        applied = await agent.replay_missed_cashera_charges(db, result['local_id'])

        assert applied == 1
        assert await _end_date(db, subscription.id) - before == timedelta(days=30)
        # Повторная сверка ничего не доначисляет.
        assert await agent.replay_missed_cashera_charges(db, result['local_id']) == 0


@pytest.mark.asyncio
async def test_recurring_charge_is_not_treated_as_topup(monkeypatch, stub):
    """Списание приходит с чужим external_id — оно не должно падать в поиск пополнения."""
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, tariff, subscription = await _seed(db)
        await _enable(db, stub, tariff, subscription, user_id)
        await cashera_module._CasheraRecurrentAgent().process_cashera_webhook(db, _charge_event('ch-1'))
        balance = (await db.execute(select(User.balance_kopeks).where(User.id == user_id))).scalar_one()

    assert balance == 0  # продление напрямую, без зачисления на баланс


# --- покупка тарифа через привязку ---------------------------------------------------


@pytest.mark.asyncio
async def test_purchase_without_subscription_creates_expired_placeholder(monkeypatch, stub):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    async with memory_session(monkeypatch, TABLES) as db:
        user = User(telegram_id=778, username='n', status=UserStatus.ACTIVE.value, language='ru', balance_kopeks=0)
        tariff = Tariff(name='Pro', is_active=True, device_limit=1, traffic_limit_gb=0, period_prices={'30': 30000})
        db.add_all([user, tariff])
        await db.commit()

        result = await cashera_module.purchase_tariff_with_cashera_recurring(db, user=user, tariff=tariff)
        placeholder = await db.get(Subscription, result['subscription_id'])

    assert placeholder.status == SubscriptionStatus.EXPIRED.value
    assert result['redirect_url'] == 'https://pay.cashera.cash/sub'
