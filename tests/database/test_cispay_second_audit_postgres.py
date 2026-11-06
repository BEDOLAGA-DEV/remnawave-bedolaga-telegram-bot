"""Adversarial recurring ledger and cancellation checks on PostgreSQL."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.database.models import Base, CisPaySubscription, CisPaySubscriptionCharge, Subscription, Tariff, User
from app.services import cispay_recurrent_service as service
from tests.database.test_cispay_recurrent_charges_postgres import payment, seed
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('already_paid', [False, True])
@pytest.mark.parametrize(
    'invalid', [{'is_sandbox': True}, {'amount': 1}, {'currency': 'USD'}, {'payment_method': 'CARD'}]
)
async def test_invalid_refund_never_mutates_ledger(postgres_database, monkeypatch, already_paid, invalid):
    monkeypatch.setattr(service, 'deliver_cispay_charge', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        if already_paid:
            await service.process_cispay_subscription_charge(db, payment())
        with pytest.raises(ValueError, match='Invalid CisPay recurring charge'):
            await service.process_cispay_subscription_charge(db, payment(status='REFUNDED', **invalid))
        await db.rollback()
        if already_paid:
            assert (await db.scalar(select(CisPaySubscriptionCharge))).status == 'APPLIED'
        else:
            assert await db.scalar(select(func.count()).select_from(CisPaySubscriptionCharge)) == 0
        # A malformed refund must not suppress the subsequent legitimate PAID.
        await service.process_cispay_subscription_charge(db, payment())
        assert (await db.scalar(select(CisPaySubscriptionCharge))).status == 'APPLIED'


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('same_tariff', [False, True])
async def test_reconcile_cancels_competing_paid_target_before_next_charge(
    postgres_database, monkeypatch, multi, same_tariff
):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        (await db.get(Subscription, 1)).status = 'expired'
        db.add(Tariff(id=2, name='Other', device_limit=2, period_prices={'30': 10000}))
        await db.flush()
        db.add(
            Subscription(
                id=2,
                user_id=1,
                tariff_id=1 if same_tariff else 2,
                remnawave_short_id='masked-2',
                status='active',
                is_trial=False,
                device_limit=2,
                end_date=datetime.now(UTC) + timedelta(days=30),
            )
        )
        await db.commit()
        await service._queue_changed_agreement(db, 1)
        agreement = await db.get(CisPaySubscription, 1)
        assert agreement.status == ('ACTIVE' if multi and not same_tariff else 'CANCEL_PENDING')


@pytest.mark.parametrize('price', [5000, 15000])
@pytest.mark.parametrize('reconcile_first', [False, True])
async def test_price_change_and_late_payment_keep_money_once(postgres_database, monkeypatch, price, reconcile_first):
    monkeypatch.setattr(service, 'deliver_cispay_charge', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        expiry = (await db.get(Subscription, 1)).end_date
        (await db.get(Tariff, 1)).period_prices = {'30': price}
        await db.commit()
        if reconcile_first:
            await service._queue_changed_agreement(db, 1)
            assert (await db.get(CisPaySubscription, 1)).status == 'CANCEL_PENDING'
        await service.process_cispay_subscription_charge(db, payment())
        await service.process_cispay_subscription_charge(db, payment())
        assert (await db.get(User, 1)).balance_kopeks == 10000
        assert (await db.get(Subscription, 1)).end_date == expiry
        assert (await db.get(CisPaySubscription, 1)).status == 'CANCEL_PENDING'
        assert await db.scalar(select(func.count()).select_from(CisPaySubscriptionCharge)) == 1


@pytest.mark.parametrize('action', ['status', 'cancel'])
async def test_foreign_subscription_has_no_access(postgres_database, action):
    from app.services.cispay_recurrent_checkout import get_cispay_agreement_state

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        db.add(User(id=2, telegram_id=2, status='active'))
        await db.commit()
        with pytest.raises(ValueError, match='subscription_not_found'):
            if action == 'status':
                await get_cispay_agreement_state(db, user_id=2, subscription_id=1)
            else:
                await service.cancel_cispay_agreement(db, user_id=2, subscription_id=1, reason='customer')
        await db.rollback()
        assert (await db.get(CisPaySubscription, 1)).status == 'ACTIVE'
