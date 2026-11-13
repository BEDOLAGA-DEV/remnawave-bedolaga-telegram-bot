"""PayHot guest/gift durable acceptance and shared recovery proof on PostgreSQL."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.database.models import Base, GuestPurchase, PayHotPayment, Transaction, User
from app.services.guest_purchase_service import _find_succeeded_provider_payment
from app.services.payhot_service import payhot_service
from app.services.payment.payhot import PayHotPaymentMixin
from tests.database.test_payhot_payments_postgres import remote, seed
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.fixture
def service(monkeypatch):
    service = PayHotPaymentMixin()
    monkeypatch.setattr(service, '_fulfill_payhot', AsyncMock())
    monkeypatch.setattr(payhot_service, 'get_payment', AsyncMock(return_value={'payment': remote()}))
    return service


async def guest_seed(db, *, gift=False, **purchase_changes):
    local_id = await seed(db, metadata_json={'purpose': 'guest_purchase', 'purchase_token': 'purchase-example'})
    values = {
        'token': 'purchase-example',
        'contact_type': 'email',
        'contact_value': 'buyer@example.com',
        'period_days': 30,
        'amount_kopeks': 10000,
        'currency': 'RUB',
        'payment_method': 'payhot_card',
        'is_gift': gift,
        'status': 'pending',
        **purchase_changes,
    }
    db.add(GuestPurchase(**values))
    await db.commit()
    return local_id


@pytest.mark.parametrize('gift', [False, True])
async def test_concurrent_guest_callbacks_commit_paid_proof_without_balance_credit(postgres_database, service, gift):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=3) as sessions:
        local_id = await guest_seed(sessions[0], gift=gift)
        results = await asyncio.gather(
            *(
                service.process_payhot_callback(
                    db, {'event_type': 'payment.succeeded', 'event_id': f'event-{index}', 'data': remote()}
                )
                for index, db in enumerate(sessions)
            )
        )
        assert all(results)
        db = sessions[0]
        purchase = (await db.scalars(select(GuestPurchase))).one()
        payment = await db.get(PayHotPayment, local_id, populate_existing=True)
        assert purchase.status == 'paid' and purchase.paid_at and purchase.payment_id == 'order-1'
        assert payment.is_paid and payment.transaction_id is None
        assert payment.fulfillment_state == {'kind': 'guest', 'target_id': purchase.id, 'state': 'pending'}
        assert (await db.get(User, 1, populate_existing=True)).balance_kopeks == 500
        assert not (await db.scalars(select(Transaction))).all()
        assert len((await db.scalars(select(PayHotPayment))).all()) == 1
        assert await _find_succeeded_provider_payment(db, 'payhot', purchase.token) == ('order-1', 10000)
        assert await _find_succeeded_provider_payment(db, 'payhot', 'foreign-purchase') is None


@pytest.mark.parametrize(
    'changes',
    [{'payment_id': 'another-order'}, {'amount_kopeks': 9999}, {'currency': 'EUR'}, {'payment_method': 'anore_card'}],
)
async def test_foreign_or_mismatched_guest_cannot_be_accepted(postgres_database, service, changes):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, check):
        local_id = await guest_seed(db, **changes)
        with pytest.raises(ValueError, match='guest purchase mismatch'):
            await service.process_payhot_callback(db, {'event_type': 'payment.succeeded', 'data': remote()})
        assert not (await check.get(PayHotPayment, local_id)).is_paid
        assert (await check.scalar(select(GuestPurchase))).status == 'pending'
        assert (await check.get(User, 1)).balance_kopeks == 500
        assert await _find_succeeded_provider_payment(check, 'payhot', 'purchase-example') is None
        service._fulfill_payhot.assert_not_awaited()


@pytest.mark.parametrize('gift', [False, True])
async def test_test_guest_events_are_rejected_without_paid_recovery_proof(postgres_database, service, gift):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        local_id = await guest_seed(db, gift=gift)
        with pytest.raises(ValueError, match='environment mismatch'):
            await service.process_payhot_callback(db, {'event_type': 'sandbox.payment.succeeded', 'data': remote()})
        payment = await db.get(PayHotPayment, local_id)
        assert payment.status == 'creating' and not payment.is_paid and payment.transaction_id is None
        purchase = await db.scalar(select(GuestPurchase))
        assert purchase.status == 'pending' and purchase.paid_at is None
        assert await _find_succeeded_provider_payment(db, 'payhot', purchase.token) is None
        assert (await db.get(User, 1)).balance_kopeks == 500
        assert not (await db.scalars(select(Transaction))).all()
        service._fulfill_payhot.assert_not_awaited()
