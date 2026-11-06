"""CisPay financial acceptance on real PostgreSQL; provider transport is fake."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.database.models import Base, CisPayPayment, Transaction, User
from app.services.cispay_service import cispay_service
from app.services.payment.cispay import CisPayPaymentMixin
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


def payload(**updates):
    return {
        'id': 'masked-invoice',
        'store_id': 'masked-shop',
        'order_id': 'masked-order',
        'payment_method': 'CARD',
        'status': 'PAID',
        'amount': 10001,
        'currency': 'RUB',
        'is_sandbox': False,
        **updates,
    }


async def seed(db, **updates):
    db.add(User(id=1, telegram_id=1, first_name='Test', balance_kopeks=500))
    await db.flush()
    payment = CisPayPayment(
        user_id=1,
        order_id='masked-order',
        amount_kopeks=10001,
        cispay_payment_id='masked-invoice',
        payment_method='CARD',
        currency='RUB',
        metadata_json={'store_id': 'masked-shop'},
        **updates,
    )
    db.add(payment)
    await db.commit()
    return payment


@pytest.fixture
def service(monkeypatch):
    actor = CisPayPaymentMixin()
    monkeypatch.setattr(actor, '_after_cispay_payment', AsyncMock(), raising=False)
    return actor


async def test_concurrent_callback_and_status_credit_once(postgres_database, service, monkeypatch):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=3) as sessions:
        await seed(sessions[0])
        monkeypatch.setattr(cispay_service, 'check_payment', AsyncMock(return_value=payload()))
        results = await asyncio.gather(
            service.process_cispay_callback(sessions[0], payload()),
            service.process_cispay_callback(sessions[1], payload()),
            service.check_cispay_payment_status(sessions[2], 'masked-order'),
        )
        assert all(results)
        assert (await sessions[0].get(User, 1, populate_existing=True)).balance_kopeks == 10501
        assert len((await sessions[0].scalars(select(Transaction))).all()) == 1
        service._after_cispay_payment.assert_awaited_once()


@pytest.mark.parametrize(
    'update',
    [
        {'amount': True},
        {'amount': 10001.9},
        {'amount': 10002},
        {'currency': 'USD'},
        {'store_id': 'foreign'},
        {'id': 'foreign'},
        {'payment_method': 'SBP'},
    ],
)
async def test_invalid_identity_or_amount_cannot_credit(postgres_database, service, update):
    from app.services.payment.cispay import CisPayValidationError

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db)
        with pytest.raises(CisPayValidationError):
            await service.process_cispay_callback(db, payload(**update))
        assert (await other.get(User, 1)).balance_kopeks == 500
        assert not (await other.scalars(select(Transaction))).all()


@pytest.mark.parametrize('update', [{'is_sandbox': True}, {'block_reason': 'blocked'}])
async def test_sandbox_and_blocked_never_credit(postgres_database, service, update):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db)
        assert await service.process_cispay_callback(db, payload(**update))
        payment = (await other.scalars(select(CisPayPayment))).one()
        assert payment.is_sandbox
        assert (await other.get(User, 1)).balance_kopeks == 500
        assert not (await other.scalars(select(Transaction))).all()
        service._after_cispay_payment.assert_not_awaited()


@pytest.fixture(autouse=True)
def configured_store(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, 'CISPAY_SHOP_ID', 'masked-shop')
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'cis_sec_example')
    monkeypatch.setattr(settings, 'CISPAY_ENABLED', True)


@pytest.mark.parametrize('source_amount', [10001, 10002, True, None])
async def test_crypto_callback_uses_verified_ruble_source(postgres_database, service, monkeypatch, source_amount):
    from app.services.payment.cispay import CisPayValidationError

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        p = await seed(db)
        p.payment_method = 'CRYPTO'
        await db.commit()
        incoming = payload(payment_method='CRYPTO', amount=111, currency='USD')
        remote = {**incoming, 'source_currency': 'RUB', 'source_amount': source_amount}
        remote.pop('store_id')
        monkeypatch.setattr(cispay_service, 'check_payment', AsyncMock(return_value=remote))
        if type(source_amount) is int and source_amount == 10001:
            assert await service.process_cispay_callback(db, incoming)
            assert (await other.get(User, 1)).balance_kopeks == 10501
        else:
            with pytest.raises(CisPayValidationError):
                await service.process_cispay_callback(db, incoming)
            assert (await other.get(User, 1)).balance_kopeks == 500
        cispay_service.check_payment.assert_awaited_once()


async def test_polling_cannot_credit_a_different_order(postgres_database, service, monkeypatch):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db)
        db.add(
            CisPayPayment(
                user_id=1,
                order_id='foreign-order',
                cispay_payment_id='foreign-invoice',
                amount_kopeks=10001,
                currency='RUB',
                payment_method='CARD',
            )
        )
        await db.commit()
        monkeypatch.setattr(
            cispay_service,
            'check_payment',
            AsyncMock(return_value=payload(id='foreign-invoice', order_id='foreign-order')),
        )
        assert not (await service.check_cispay_payment_status(db, 'masked-order'))['is_paid']
        assert (await other.get(User, 1)).balance_kopeks == 500
        assert not (await other.scalars(select(Transaction))).all()


async def test_polling_failure_reserves_rate_gate(postgres_database, service, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.services.cispay_service import CisPayAPIError

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db)
        check = AsyncMock(side_effect=CisPayAPIError(503, retry_after=12))
        monkeypatch.setattr(cispay_service, 'check_payment', check)
        await service.check_cispay_payment_status(db, 'masked-order')
        await service.check_cispay_payment_status(other, 'masked-order')
        check.assert_awaited_once()
        p = (await other.scalars(select(CisPayPayment))).one()
        assert p.next_check_at > datetime.now(UTC) + timedelta(seconds=10)


async def test_failed_commit_then_retry_is_atomic(postgres_database, service, monkeypatch):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db)
        commit = db.commit
        monkeypatch.setattr(db, 'commit', AsyncMock(side_effect=RuntimeError('synthetic failure')))
        with pytest.raises(RuntimeError):
            await service.process_cispay_callback(db, payload())
        assert (await other.get(User, 1)).balance_kopeks == 500
        assert not (await other.scalars(select(CisPayPayment))).one().is_paid
        assert not (await other.scalars(select(Transaction))).all()
        await other.rollback()
        monkeypatch.setattr(db, 'commit', commit)
        assert await service.process_cispay_callback(db, payload())
        assert (await other.get(User, 1)).balance_kopeks == 10501


@pytest.mark.parametrize('gift', [False, True])
async def test_early_guest_callback_is_durable_before_create_returns(postgres_database, service, monkeypatch, gift):
    from app.database.models import GuestPurchase

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        db.add(
            GuestPurchase(
                token='masked-token',
                contact_type='email',
                contact_value='test@example.com',
                period_days=30,
                amount_kopeks=10001,
                payment_method='cispay_card',
                is_gift=gift,
            )
        )
        monkeypatch.setattr(
            cispay_service,
            'get_store_capabilities',
            AsyncMock(
                return_value={
                    'store_id': 'masked-shop',
                    'is_active': True,
                    'payment_methods': [{'payment_method': 'CARD', 'is_active': True}],
                }
            ),
        )
        service._after_cispay_payment.side_effect = RuntimeError('synthetic fulfillment unavailable')

        async def create(**kwargs):
            assert (await other.scalars(select(GuestPurchase))).one().token == 'masked-token'
            assert await service.process_cispay_callback(other, payload(order_id=kwargs['order_id']))
            return {
                'id': 'masked-invoice',
                'order_id': kwargs['order_id'],
                'status': 'PENDING',
                'amount': 10001,
                'charged_amount': 10300,
                'payment_url': 'https://pay.example/invoice',
            }

        monkeypatch.setattr(cispay_service, 'create_payment', create)
        result = await service.create_cispay_payment(
            db,
            user_id=None,
            amount_kopeks=10001,
            payment_method_type='card',
            metadata={'purpose': 'guest_purchase', 'purchase_token': 'masked-token'},
        )
        assert result
        purchase = (await db.scalars(select(GuestPurchase))).one()
        await db.refresh(purchase)
        assert purchase.status == 'paid' and purchase.payment_id == result['order_id']
        payment = (await db.scalars(select(CisPayPayment))).one()
        assert payment.is_paid and payment.status == 'success'
        assert payment.expires_at is None  # create does not promise a 30 minute TTL
        assert not (await db.scalars(select(Transaction))).all()


async def test_expired_or_refunded_does_not_undo_credit(postgres_database, service):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db)
        for status in ['PAID', 'EXPIRED', 'REFUNDED', 'PAID']:
            assert await service.process_cispay_callback(db, payload(status=status))
        assert (await other.get(User, 1)).balance_kopeks == 10501
        assert len((await other.scalars(select(Transaction))).all()) == 1
        assert (await other.scalars(select(CisPayPayment))).one().is_paid
        service._after_cispay_payment.assert_awaited_once()


async def test_uncredited_refunded_invoice_cannot_later_credit(postgres_database, service):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db)
        assert await service.process_cispay_callback(db, payload(status='REFUNDED'))
        assert await service.process_cispay_callback(db, payload())
        assert (await other.get(User, 1)).balance_kopeks == 500
        assert not (await other.scalars(select(Transaction))).all()
        assert (await other.scalars(select(CisPayPayment))).one().status == 'refunded'


async def test_crypto_duplicate_reuses_accepted_source_without_polling(postgres_database, service, monkeypatch):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        p = await seed(db)
        p.payment_method = 'CRYPTO'
        await db.commit()
        incoming = payload(payment_method='CRYPTO', amount=111, currency='USD')
        remote = {**incoming, 'source_currency': 'RUB', 'source_amount': 10001}
        monkeypatch.setattr(cispay_service, 'check_payment', AsyncMock(return_value=remote))
        assert await service.process_cispay_callback(db, incoming)
        assert await service.process_cispay_callback(db, incoming)
        cispay_service.check_payment.assert_awaited_once()
        assert (await other.get(User, 1)).balance_kopeks == 10501
        assert (await other.scalars(select(CisPayPayment))).one().charged_amount_kopeks is None


@pytest.mark.parametrize('missing,sandbox', [(True, False), (False, True)])
async def test_guest_missing_or_sandbox_never_issues_purchase(postgres_database, service, missing, sandbox):
    from app.database.models import GuestPurchase
    from app.services.payment.cispay import CisPayValidationError

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        p = await seed(db)
        p.metadata_json = {**p.metadata_json, 'purpose': 'guest_purchase', 'purchase_token': 'masked-token'}
        if not missing:
            db.add(
                GuestPurchase(
                    token='masked-token',
                    contact_type='email',
                    contact_value='test@example.com',
                    period_days=30,
                    amount_kopeks=10001,
                    payment_method='cispay_card',
                )
            )
        await db.commit()
        if missing:
            with pytest.raises(CisPayValidationError):
                await service.process_cispay_callback(db, payload())
        else:
            assert await service.process_cispay_callback(db, payload(is_sandbox=sandbox))
            assert (await other.scalars(select(GuestPurchase))).one().status == 'pending'
        assert (await other.get(User, 1)).balance_kopeks == 500
        assert not (await other.scalars(select(Transaction))).all()
        service._after_cispay_payment.assert_not_awaited()


async def test_create_timeout_keeps_order_for_late_callback(postgres_database, service, monkeypatch):
    from app.services.cispay_service import CisPayAPIError

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        db.add(User(id=1, telegram_id=1, first_name='Test', balance_kopeks=500))
        await db.commit()
        monkeypatch.setattr(
            cispay_service,
            'get_store_capabilities',
            AsyncMock(
                return_value={
                    'store_id': 'masked-shop',
                    'is_active': True,
                    'payment_methods': [{'payment_method': 'CARD', 'is_active': True}],
                }
            ),
        )
        create = AsyncMock(side_effect=CisPayAPIError(None))
        monkeypatch.setattr(cispay_service, 'create_payment', create)
        assert await service.create_cispay_payment(db, user_id=1, amount_kopeks=10001) is None
        draft = (await other.scalars(select(CisPayPayment))).one()
        assert draft.cispay_payment_id is None and not draft.is_paid
        assert await service.process_cispay_callback(other, payload(order_id=draft.order_id))
        assert (await other.get(User, 1)).balance_kopeks == 10501
        create.assert_awaited_once()


async def test_status_provider_expiration_replaces_no_default_ttl(postgres_database, service, monkeypatch):
    from datetime import UTC, datetime, timedelta

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db)
        remote = payload(status='PENDING', expires_in_seconds=7200)
        remote.pop('store_id')
        monkeypatch.setattr(cispay_service, 'check_payment', AsyncMock(return_value=remote))
        await service.check_cispay_payment_status(db, 'masked-order')
        p = (await other.scalars(select(CisPayPayment))).one()
        assert datetime.now(UTC) + timedelta(minutes=119) < p.expires_at < datetime.now(UTC) + timedelta(minutes=121)


async def test_migration_upgrade_preserves_historical_payment(postgres_database):
    from importlib import import_module

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    from tests.fixtures.postgres_db import postgres_engine

    migration = import_module('migrations.alembic.versions.0136_cispay_sandbox_and_polling')
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    'INSERT INTO cispay_payments(order_id, amount_kopeks, currency, status, is_paid) '
                    "VALUES ('masked-old', 10001, 'RUB', 'success', true)"
                )
            )
            await conn.execute(text('ALTER TABLE cispay_payments DROP COLUMN is_sandbox, DROP COLUMN next_check_at'))

            def upgrade(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    migration.upgrade()
                    migration.upgrade()

            await conn.run_sync(upgrade)
            row = (await conn.execute(text('SELECT is_paid, is_sandbox, next_check_at FROM cispay_payments'))).one()
            assert row.is_paid is True and row.is_sandbox is False and row.next_check_at is None
            await conn.execute(text('UPDATE cispay_payments SET is_sandbox = true'))

            def downgrade(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    with pytest.raises(RuntimeError, match='sandbox provenance'):
                        migration.downgrade()

            await conn.run_sync(downgrade)


@pytest.mark.parametrize('status, expected', [('EXPIRED', 'expired'), ('FAILED', 'declined'), ('REFUNDED', 'refunded')])
async def test_unpaid_crypto_terminal_webhook_needs_no_conversion_query(
    postgres_database, service, monkeypatch, status, expected
):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        payment = await seed(db)
        payment.payment_method = 'CRYPTO'
        await db.commit()
        check = AsyncMock()
        monkeypatch.setattr(cispay_service, 'check_payment', check)
        assert await service.process_cispay_callback(
            db, payload(payment_method='CRYPTO', currency='USD', amount=111, status=status)
        )
        check.assert_not_awaited()
        assert (await other.scalars(select(CisPayPayment))).one().status == expected
        assert (await other.get(User, 1)).balance_kopeks == 500
