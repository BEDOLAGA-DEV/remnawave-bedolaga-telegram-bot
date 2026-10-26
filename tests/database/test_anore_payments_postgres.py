"""Anore money invariants with independent PostgreSQL connections."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import AnorePayment, Base, GuestPurchase, Transaction, User
from app.services.anore_service import AnoreAPIError, anore_service
from app.services.payment.anore import AnorePaymentMixin, AnoreValidationError
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


def payload(**updates):
    return {
        'event': 'payment.succeeded',
        'id': 'invoice-1',
        'orderId': 'order-1',
        'amount': 100.01,
        'rubAmount': 100.01,
        'currency': 'rub',
        'status': 'paid',
        '_delivery_id': 'delivery-1',
        **updates,
    }


async def seed(db, **updates):
    user = User(id=1, telegram_id=1, first_name='Test', balance_kopeks=500)
    db.add(user)
    await db.flush()
    payment = AnorePayment(user_id=1, order_id='order-1', amount_kopeks=10001, anore_payment_id='invoice-1', **updates)
    db.add(payment)
    await db.commit()
    return payment


@pytest.fixture
def service(monkeypatch):
    service = AnorePaymentMixin()
    monkeypatch.setattr(service, '_after_anore_payment', AsyncMock())
    return service


async def test_concurrent_callbacks_credit_once(postgres_database, service):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=3) as sessions:
        await seed(sessions[0])
        results = await asyncio.gather(
            *[
                service.process_anore_callback(db, payload(_delivery_id=f'delivery-{i}'))
                for i, db in enumerate(sessions)
            ]
        )
        assert all(results)
        db = sessions[0]
        assert (await db.get(User, 1, populate_existing=True)).balance_kopeks == 10501
        assert len((await db.scalars(select(Transaction))).all()) == 1
        p = (await db.scalars(select(AnorePayment))).one()
        assert p.is_paid and p.transaction_id and p.status == 'paid'
        assert service._after_anore_payment.await_count == 1


@pytest.mark.parametrize(
    'updates',
    [
        {'amount': True},
        {'amount': 100.001},
        {'amount': 1},
        {'rubAmount': 100},
        {'currency': 'USD'},
        {'id': 'foreign'},
        {'orderId': 'foreign'},
        {'status': 'expired'},
        {'event': 'payment.expired'},
        {'id': None},
    ],
)
async def test_invalid_callback_cannot_credit(postgres_database, service, updates):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db)
        with pytest.raises(AnoreValidationError):
            await service.process_anore_callback(db, payload(**updates))
        assert (await other.get(User, 1)).balance_kopeks == 500
        assert not (await other.scalars(select(AnorePayment))).one().is_paid
        assert not (await other.scalars(select(Transaction))).all()


async def test_paid_never_reversed_and_sandbox_never_credits(postgres_database, service):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db, is_test=True)
        assert await service.process_anore_callback(db, payload())
        assert await service.process_anore_callback(db, payload(event='payment.expired', status='expired'))
        p = (await other.scalars(select(AnorePayment))).one()
        assert p.status == 'paid' and p.is_paid
        assert (await other.get(User, 1)).balance_kopeks == 500
        assert not (await other.scalars(select(Transaction))).all()
        service._after_anore_payment.assert_not_awaited()


async def test_financial_commit_failure_rolls_back_then_retry(postgres_database, service, monkeypatch):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db)
        original = db.commit
        monkeypatch.setattr(db, 'commit', AsyncMock(side_effect=RuntimeError('synthetic failure')))
        with pytest.raises(RuntimeError):
            await service.process_anore_callback(db, payload())
        assert (await other.get(User, 1)).balance_kopeks == 500
        assert not (await other.scalars(select(AnorePayment))).one().is_paid
        await other.rollback()
        monkeypatch.setattr(db, 'commit', original)
        assert await service.process_anore_callback(db, payload())
        assert (await other.get(User, 1, populate_existing=True)).balance_kopeks == 10501


@pytest.mark.parametrize('gift', [False, True])
async def test_guest_accepted_atomically_without_balance(postgres_database, service, gift):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db, metadata_json={'purpose': 'guest_purchase', 'purchase_token': 'guest-token'})
        db.add(
            GuestPurchase(
                token='guest-token',
                contact_type='email',
                contact_value='test@example.com',
                period_days=30,
                amount_kopeks=10001,
                payment_method='anore_card',
                is_gift=gift,
            )
        )
        await db.commit()
        assert await service.process_anore_callback(db, payload())
        guest = (await other.scalars(select(GuestPurchase))).one()
        assert guest.status == 'paid' and guest.paid_at and guest.payment_id == 'order-1'
        assert (await other.get(User, 1)).balance_kopeks == 500
        assert not (await other.scalars(select(Transaction))).all()


async def test_missing_guest_never_falls_back_to_topup(postgres_database, service):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db, metadata_json={'purpose': 'guest_purchase', 'purchase_token': 'missing'})
        with pytest.raises(AnoreValidationError):
            await service.process_anore_callback(db, payload())
        assert (await other.get(User, 1)).balance_kopeks == 500


async def test_status_failure_preserves_polling_gate(postgres_database, service, monkeypatch):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db)
        get = AsyncMock(side_effect=AnoreAPIError(status_code=429, retry_after=12))
        monkeypatch.setattr(anore_service, 'get_payment', get)
        await service.check_anore_payment_status(db, 'order-1')
        await service.check_anore_payment_status(other, 'order-1')
        assert get.await_count == 1
        p = (await other.scalars(select(AnorePayment))).one()
        assert p.next_check_at > datetime.now(UTC) + timedelta(seconds=10)


async def test_early_callback_sees_committed_guest_and_is_not_overwritten(postgres_database, service, monkeypatch):
    monkeypatch.setattr(settings, 'ANORE_ENABLED', True)
    monkeypatch.setattr(settings, 'ANORE_API_KEY', 'an_live_example')
    monkeypatch.setattr(settings, 'ANORE_SECRET_KEY', 'example')
    monkeypatch.setattr(settings, 'ANORE_SHOP_ID', 1)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        db.add(
            GuestPurchase(
                token='guest-token',
                contact_type='email',
                contact_value='test@example.com',
                period_days=30,
                amount_kopeks=10001,
                payment_method='anore',
                is_gift=True,
            )
        )

        async def create(**kwargs):
            order = kwargs['order_id']
            assert (await other.scalars(select(GuestPurchase))).one().token == 'guest-token'
            assert await service.process_anore_callback(other, payload(orderId=order))
            return {
                **payload(orderId=order, status='new'),
                'success': True,
                'paymentUrl': 'https://pay.example.com/invoice',
                'expiresIn': 14400,
            }

        monkeypatch.setattr(anore_service, 'create_payment', create)
        result = await service.create_anore_payment(
            db,
            user_id=None,
            amount_kopeks=10001,
            description='Test',
            metadata={'purpose': 'guest_purchase', 'purchase_token': 'guest-token'},
        )
        assert result and result['status'] == 'paid'
        assert (await db.scalars(select(GuestPurchase))).one().status == 'paid'


async def test_real_postcommit_orchestration_continues_after_notification_failure(postgres_database, monkeypatch):
    from app.database.crud import transaction as transaction_crud
    from app.services import referral_service
    from app.services.admin_notification_service import AdminNotificationService
    from app.services.payment import common

    service = AnorePaymentMixin()
    service.bot = object()
    service._send_payment_success_notification = AsyncMock(side_effect=RuntimeError('notification unavailable'))
    emit = AsyncMock()
    referral = AsyncMock()
    cart = AsyncMock()
    admin = AsyncMock()
    monkeypatch.setattr(transaction_crud, 'emit_transaction_side_effects', emit)
    monkeypatch.setattr(referral_service, 'process_referral_topup', referral)
    monkeypatch.setattr(common, 'send_cart_notification_after_topup', cart)
    monkeypatch.setattr(AdminNotificationService, 'send_balance_topup_notification', admin)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db)
        assert await service.process_anore_callback(db, payload())
        assert (await other.get(User, 1)).balance_kopeks == 10501
        assert (await other.get(User, 1)).has_made_first_topup
        emit.assert_awaited_once()
        referral.assert_awaited_once()
        admin.assert_awaited_once()
        cart.assert_awaited_once()


async def test_callback_races_polling_without_double_deposit(postgres_database, service, monkeypatch):
    monkeypatch.setattr(anore_service, 'get_payment', AsyncMock(return_value={**payload(), 'paid': True}))
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db)
        await asyncio.gather(
            service.process_anore_callback(db, payload()), service.check_anore_payment_status(other, 'order-1')
        )
        assert (await db.get(User, 1, populate_existing=True)).balance_kopeks == 10501
        assert len((await db.scalars(select(Transaction))).all()) == 1


@pytest.mark.parametrize('terminal', ['delivered', 'pending_activation', 'failed', 'paid'])
async def test_guest_replay_never_rewinds_status_or_paid_at(postgres_database, service, terminal):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db, metadata_json={'purpose': 'guest_purchase', 'purchase_token': 'guest-token'})
        paid = datetime.now(UTC) - timedelta(days=1)
        db.add(
            GuestPurchase(
                token='guest-token',
                contact_type='email',
                contact_value='test@example.com',
                period_days=30,
                amount_kopeks=10001,
                payment_method='anore',
                status=terminal,
                paid_at=paid,
                payment_id='order-1',
            )
        )
        await db.commit()
        assert await service.process_anore_callback(db, payload())
        guest = (await other.scalars(select(GuestPurchase))).one()
        assert (guest.status, guest.paid_at) == (terminal, paid)
        service._after_anore_payment.assert_not_awaited()


async def test_soft_deleted_user_is_not_credited(postgres_database, service):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db)
        (await db.get(User, 1)).status = 'deleted'
        await db.commit()
        with pytest.raises(AnoreValidationError):
            await service.process_anore_callback(db, payload())
        assert (await other.get(User, 1)).balance_kopeks == 500


async def test_retry_after_starts_at_response_receipt(postgres_database, service, monkeypatch):
    from app.services.payment import anore as module

    current = datetime.now(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current

    monkeypatch.setattr(module, 'datetime', Clock)

    async def slow_error(*args, **kwargs):
        nonlocal current
        current += timedelta(seconds=10)
        raise AnoreAPIError(status_code=429, retry_after=5)

    monkeypatch.setattr(anore_service, 'get_payment', slow_error)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db)
        await service.check_anore_payment_status(db, 'order-1')
        p = (await other.scalars(select(AnorePayment))).one()
        assert p.next_check_at >= current + timedelta(seconds=5)


async def test_pending_invoice_follows_actual_account_merge(postgres_database, service):
    from app.services.account_merge_service import execute_merge

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db)
        db.add(User(id=2, email='primary@example.com', balance_kopeks=200))
        await db.commit()
        await execute_merge(db, primary_user_id=2, secondary_user_id=1)
        await db.commit()
        assert await service.process_anore_callback(other, payload())
        assert (await db.get(User, 2, populate_existing=True)).balance_kopeks == 10701
        payment = (await db.scalars(select(AnorePayment).execution_options(populate_existing=True))).one()
        assert payment.user_id == 2
        assert (await db.scalars(select(Transaction))).one().user_id == 2


@pytest.mark.parametrize(
    'field,value',
    [
        ('is_enabled', False),
        ('sub_options', {'card': False, 'sbp': False, 'yoomoney': False, 'crypto': False}),
        ('max_amount_kopeks', 10000),
    ],
)
async def test_db_policy_rejects_before_remote_creation(postgres_database, service, monkeypatch, field, value):
    from app.database.models import PaymentMethodConfig

    monkeypatch.setattr(settings, 'ANORE_ENABLED', True)
    monkeypatch.setattr(settings, 'ANORE_API_KEY', 'an_live_example')
    monkeypatch.setattr(settings, 'ANORE_SECRET_KEY', 'example')
    monkeypatch.setattr(settings, 'ANORE_SHOP_ID', 1)
    remote = AsyncMock()
    monkeypatch.setattr(anore_service, 'create_payment', remote)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        db.add(User(id=1, telegram_id=1, first_name='Test'))
        config = PaymentMethodConfig(method_id='anore', is_enabled=True)
        setattr(config, field, value)
        db.add(config)
        await db.commit()
        assert await service.create_anore_payment(db, user_id=1, amount_kopeks=10001) is None
        remote.assert_not_awaited()
        assert not (await other.scalars(select(AnorePayment))).all()


async def test_enabled_submethods_are_sent_as_restricted_list(postgres_database, service, monkeypatch):
    from app.database.models import PaymentMethodConfig

    monkeypatch.setattr(settings, 'ANORE_ENABLED', True)
    monkeypatch.setattr(settings, 'ANORE_API_KEY', 'an_live_example')
    monkeypatch.setattr(settings, 'ANORE_SECRET_KEY', 'example')
    monkeypatch.setattr(settings, 'ANORE_SHOP_ID', 1)

    async def remote(**kwargs):
        assert kwargs['methods'] == ['sbp']
        return {**payload(orderId=kwargs['order_id'], status='new'), 'paymentUrl': 'https://pay.example.com/pay'}

    monkeypatch.setattr(anore_service, 'create_payment', remote)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        db.add(User(id=1, telegram_id=1, first_name='Test'))
        db.add(
            PaymentMethodConfig(
                method_id='anore',
                is_enabled=True,
                sub_options={'card': False, 'sbp': True, 'yoomoney': False, 'crypto': False},
            )
        )
        await db.commit()
        assert await service.create_anore_payment(db, user_id=1, amount_kopeks=10001)


async def test_restricted_topup_rejected_in_core(postgres_database, service, monkeypatch):
    monkeypatch.setattr(settings, 'ANORE_ENABLED', True)
    monkeypatch.setattr(settings, 'ANORE_API_KEY', 'an_live_example')
    monkeypatch.setattr(settings, 'ANORE_SECRET_KEY', 'example')
    monkeypatch.setattr(settings, 'ANORE_SHOP_ID', 1)
    remote = AsyncMock()
    monkeypatch.setattr(anore_service, 'create_payment', remote)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        db.add(User(id=1, telegram_id=1, restriction_topup=True))
        await db.commit()
        assert await service.create_anore_payment(db, user_id=1, amount_kopeks=10001) is None
        remote.assert_not_awaited()


@pytest.mark.parametrize('gift', [False, True])
async def test_guest_delivery_failure_preserves_durable_payment(postgres_database, monkeypatch, gift):
    from app.services import guest_purchase_service

    service = AnorePaymentMixin()
    fulfill = AsyncMock(side_effect=RuntimeError('panel unavailable'))
    notify = AsyncMock(side_effect=RuntimeError('delivery unavailable'))
    monkeypatch.setattr(guest_purchase_service, 'fulfill_purchase', fulfill)
    monkeypatch.setattr(guest_purchase_service, 'notify_gift_claim_available', notify)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db, metadata_json={'purpose': 'guest_purchase', 'purchase_token': 'guest-token'})
        db.add(
            GuestPurchase(
                token='guest-token',
                contact_type='email',
                contact_value='test@example.com',
                period_days=30,
                amount_kopeks=10001,
                payment_method='anore',
                is_gift=gift,
            )
        )
        await db.commit()
        assert await service.process_anore_callback(db, payload())
        assert (await other.scalars(select(GuestPurchase))).one().status == 'paid'
        assert (await other.scalars(select(AnorePayment))).one().is_paid
        assert (await other.get(User, 1)).balance_kopeks == 500
        await other.rollback()
        assert await service.process_anore_callback(db, payload())
        assert fulfill.await_count == (0 if gift else 1)
        assert notify.await_count == (1 if gift else 0)


async def test_sandbox_excluded_from_gateway_conversion_statistics(postgres_database):
    from app.database.crud.payment_gateway_stats import get_gateway_success_rates

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db, is_test=True, is_paid=True, status='paid')
        db.add(
            AnorePayment(
                user_id=1, order_id='live-order', amount_kopeks=10001, is_test=False, is_paid=True, status='paid'
            )
        )
        await db.commit()
        now = datetime.now(UTC)
        rows = await get_gateway_success_rates(db, now - timedelta(days=1), now + timedelta(days=1))
        row = next(row for row in rows if row['method'] == 'anore')
        assert row == {'method': 'anore', 'total': 1, 'paid': 1, 'success_rate': 100.0}
