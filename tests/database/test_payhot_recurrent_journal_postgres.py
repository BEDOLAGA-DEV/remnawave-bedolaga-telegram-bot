"""Durable merchant charge reservations; PostgreSQL locks and constraints."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.database.crud.payhot_recurrent import request_cancellation, reserve_charge
from app.database.models import Base, PayHotAgreement, PayHotChargeAttempt, Subscription, User
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres
END = datetime(2030, 1, 1, tzinfo=UTC)
MANDATE = '00000000-0000-4000-8000-000000000001'


def payload(**updates):
    return {
        'amount_minor': '10000',
        'currency': 'RUB',
        'payment_method': 'card',
        'mandate_id': MANDATE,
        'merchant_recurring_charge_reference': 'merchant-example-1',
        **updates,
    }


async def seed(db):
    db.add(User(id=1, email='payer@example.com'))
    await db.flush()
    db.add(Subscription(id=1, user_id=1, end_date=END, status='active'))
    await db.flush()
    agreement = PayHotAgreement(
        user_id=1,
        owner_id=1,
        subscription_id=1,
        mandate_id=MANDATE,
        checkout_id='checkout-example',
        status='ACTIVE',
        consent_snapshot={'amount_kopeks': 10000, 'currency': 'RUB', 'period_days': 30, 'payment_method': 'card'},
    )
    db.add(agreement)
    await db.commit()
    return agreement.id


async def reserve(db, agreement_id, **changes):
    values = dict(
        agreement_id=agreement_id,
        cycle_key='cycle-1',
        expected_end_date=END,
        request_payload=payload(),
        idempotency_key='idempotency-example-1',
        merchant_reference='merchant-example-1',
    )
    return await reserve_charge(db, **(values | changes))


async def test_concurrent_same_cycle_returns_one_durable_attempt(postgres_database):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=3) as sessions:
        aid = await seed(sessions[0])

        async def work(db):
            attempt = await reserve(db, aid)
            await db.commit()
            return attempt.id

        ids = await asyncio.gather(*(work(db) for db in sessions))
        assert len(set(ids)) == 1
        assert len((await sessions[0].scalars(select(PayHotChargeAttempt))).all()) == 1


@pytest.mark.parametrize(
    'change',
    [
        {'request_payload': {'amount_minor': '10001', 'currency': 'RUB', 'payment_method': 'card'}},
        {'expected_end_date': datetime(2030, 1, 2, tzinfo=UTC)},
        {'idempotency_key': 'idempotency-other-1'},
        {'merchant_reference': 'merchant-other-1'},
    ],
)
async def test_existing_cycle_cannot_change_request(postgres_database, change):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        aid = await seed(db)
        await reserve(db, aid)
        await db.commit()
        with pytest.raises(ValueError):
            await reserve(db, aid, **change)
        await db.rollback()
        assert len((await db.scalars(select(PayHotChargeAttempt))).all()) == 1


async def test_cancellation_precedes_network_and_is_idempotent(postgres_database):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        aid = await seed(db)
        first = await request_cancellation(db, aid, reason='customer')
        timestamp = first.cancel_requested_at
        await db.commit()
        again = await request_cancellation(db, aid, reason='repeat')
        assert again.cancel_requested_at == timestamp and again.reason == 'customer'
        await db.commit()
        with pytest.raises(ValueError):
            await reserve(other, aid)
        await other.rollback()
        assert not (await other.scalars(select(PayHotChargeAttempt))).all()


@pytest.mark.parametrize('status', ['CREATING', 'PENDING', 'CANCEL_PENDING', 'CANCELLED', 'FAILED'])
async def test_non_active_agreement_cannot_reserve(postgres_database, status):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        aid = await seed(db)
        agreement = await db.get(PayHotAgreement, aid)
        agreement.status = status
        await db.commit()
        with pytest.raises(ValueError):
            await reserve(db, aid)


async def test_caller_rollback_removes_reservation(postgres_database):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        aid = await seed(db)
        await reserve(db, aid)
        await db.rollback()
        assert not (await other.scalars(select(PayHotChargeAttempt))).all()


async def test_owner_deletion_preserves_financial_identity_and_attempt(postgres_database):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        aid = await seed(db)
        attempt = await reserve(db, aid)
        attempt_id = attempt.id
        await db.commit()
        await db.execute(delete(User).where(User.id == 1))
        await db.commit()
        agreement = await other.get(PayHotAgreement, aid)
        assert agreement.user_id is None and agreement.owner_id == 1
        assert (await other.get(PayHotChargeAttempt, attempt_id)).agreement_id == aid
        with pytest.raises(IntegrityError):
            await db.execute(delete(PayHotAgreement).where(PayHotAgreement.id == aid))
        await db.rollback()


@pytest.mark.parametrize('field', ['idempotency_key', 'merchant_reference', 'provider_charge_id'])
async def test_provider_financial_identity_unique_across_cycles(postgres_database, field):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        aid = await seed(db)
        first = await reserve(db, aid)
        first.provider_charge_id = '00000000-0000-4000-8000-000000000011'
        first.status = 'FAILED'
        await db.commit()
        values = dict(
            agreement_id=aid,
            cycle_key='cycle-2',
            amount_kopeks=10000,
            currency='RUB',
            expected_end_date=END + timedelta(days=30),
            request_payload={},
            idempotency_key='idempotency-example-2',
            merchant_reference='merchant-example-2',
            provider_charge_id='00000000-0000-4000-8000-000000000012',
        )
        values[field] = getattr(first, field)
        db.add(PayHotChargeAttempt(**values))
        with pytest.raises(IntegrityError) as error:
            await db.commit()
        assert error.value.orig.__cause__.constraint_name == f'payhot_charge_attempts_{field}_key'
        await db.rollback()


@pytest.mark.parametrize('field', ['user_id', 'subscription_id', 'mandate_id'])
async def test_missing_binding_cannot_create_charge(postgres_database, field):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        aid = await seed(db)
        setattr(await db.get(PayHotAgreement, aid), field, None)
        await db.commit()
        with pytest.raises(ValueError):
            await reserve(db, aid)


@pytest.mark.parametrize(
    'updates',
    [
        {'mandate_id': '00000000-0000-4000-8000-000000000002'},
        {'payment_method': 'sbp'},
        {'merchant_recurring_charge_reference': None},
    ],
)
async def test_payload_must_match_bound_mandate_and_consent(postgres_database, updates):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        aid = await seed(db)
        with pytest.raises(ValueError):
            await reserve(db, aid, request_payload=payload(**updates))


@pytest.mark.parametrize('status', ['RESERVED', 'UNKNOWN', 'PROCESSING', 'SUCCEEDED'])
async def test_unresolved_attempt_blocks_other_cycle(postgres_database, status):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        aid = await seed(db)
        attempt = await reserve(db, aid)
        attempt.status = status
        (await db.get(Subscription, 1)).end_date = END + timedelta(days=30)
        await db.commit()
        with pytest.raises(ValueError):
            await reserve(
                db,
                aid,
                cycle_key='cycle-2',
                expected_end_date=END + timedelta(days=30),
                idempotency_key='idempotency-example-2',
                merchant_reference='merchant-example-2',
                request_payload=payload(merchant_recurring_charge_reference='merchant-example-2'),
            )


async def test_unresolved_partial_index_enforces_exclusivity_without_crud(postgres_database):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        aid = await seed(db)
        await reserve(db, aid)
        await db.commit()
        db.add(
            PayHotChargeAttempt(
                agreement_id=aid,
                cycle_key='other',
                amount_kopeks=10000,
                currency='RUB',
                request_payload=payload(),
                expected_end_date=END + timedelta(days=30),
                idempotency_key='idempotency-direct-2',
                merchant_reference='direct-2',
                status='RESERVED',
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()


async def test_migration_upgrade_bootstrap_and_financial_retention(postgres_database):
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from tests.database.test_retired_gateway_migration_postgres import _load
    from tests.fixtures.postgres_db import postgres_engine

    migration = _load('0143_create_payhot_recurrent_journal.py')
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        async with engine.begin() as conn:

            def exercise(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    names = ('payhot_agreements', 'payhot_charge_attempts')
                    baseline = {
                        name: [(c['name'], str(c['type']), c['nullable']) for c in sa.inspect(sync).get_columns(name)]
                        for name in names
                    }
                    migration.upgrade()
                    migration.downgrade()
                    migration.upgrade()
                    migration.upgrade()
                    assert baseline == {
                        name: [(c['name'], str(c['type']), c['nullable']) for c in sa.inspect(sync).get_columns(name)]
                        for name in names
                    }
                    for name in names:
                        assert {i.name for i in Base.metadata.tables[name].indexes} == {
                            i['name'] for i in sa.inspect(sync).get_indexes(name) if not i.get('duplicates_constraint')
                        }
                    indexes = sa.inspect(sync).get_indexes('payhot_charge_attempts')
                    assert any(i['name'] == 'uq_payhot_charge_unresolved' and i['unique'] for i in indexes)
                    sync.execute(
                        PayHotAgreement.__table__.insert().values(
                            owner_id=1, checkout_id='retained', consent_snapshot={}, status='CANCELLED'
                        )
                    )
                    with pytest.raises(RuntimeError, match='populated'):
                        migration.downgrade()
                    assert sa.inspect(sync).has_table('payhot_charge_attempts')

            await conn.run_sync(exercise)


async def test_reservation_holds_subscription_against_manual_renewal(postgres_database):
    from sqlalchemy import text, update
    from sqlalchemy.exc import DBAPIError

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, manual):
        aid = await seed(db)
        await reserve(db, aid)
        await manual.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(DBAPIError) as error:
            await manual.execute(
                update(Subscription).where(Subscription.id == 1).values(end_date=datetime(2030, 2, 1, tzinfo=UTC))
            )
        assert error.value.orig.sqlstate == '55P03'
        await manual.rollback()
        await db.commit()
        await manual.execute(
            update(Subscription).where(Subscription.id == 1).values(end_date=datetime(2030, 2, 1, tzinfo=UTC))
        )
        await manual.commit()


async def test_account_transfer_does_not_transfer_mandate_consent(postgres_database):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        aid = await seed(db)
        db.add(User(id=2, email='new-owner@example.com'))
        await db.flush()
        agreement = await db.get(PayHotAgreement, aid)
        agreement.user_id = 2
        (await db.get(Subscription, 1)).user_id = 2
        await db.commit()
        with pytest.raises(ValueError):
            await reserve(db, aid)


async def test_manual_renewal_wins_while_reservation_waits_for_user_lock(postgres_database):
    from sqlalchemy import update

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, manual):
        aid = await seed(db)
        await manual.scalar(select(User).where(User.id == 1).with_for_update())
        await manual.execute(
            update(Subscription).where(Subscription.id == 1).values(end_date=datetime(2030, 2, 1, tzinfo=UTC))
        )
        started = asyncio.Event()
        original = db.scalar

        async def observed(statement, *args, **kwargs):
            if statement._for_update_arg is not None and 'users' in str(statement):
                started.set()
            return await original(statement, *args, **kwargs)

        db.scalar = observed
        task = asyncio.create_task(reserve(db, aid))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            assert not task.done()
            await manual.commit()
            with pytest.raises(ValueError, match='target changed'):
                await asyncio.wait_for(task, timeout=2)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await db.rollback()


@pytest.mark.parametrize('status', ['FAILED', 'CANCELLED', 'SUCCEEDED'])
async def test_alternate_cycle_key_cannot_repeat_same_billing_boundary(postgres_database, status):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        aid = await seed(db)
        attempt = await reserve(db, aid)
        attempt.status = status
        if status == 'SUCCEEDED':
            attempt.delivery_completed_at = END
        await db.commit()
        with pytest.raises(ValueError, match='boundary already reserved'):
            await reserve(
                db,
                aid,
                cycle_key='alternate',
                idempotency_key='alternate-idempotency',
                merchant_reference='alternate-ref',
                request_payload=payload(merchant_recurring_charge_reference='alternate-ref'),
            )


async def test_only_one_live_agreement_per_target_after_terminal_agreement(postgres_database):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=3) as sessions:
        aid = await seed(sessions[0])
        old = await sessions[0].get(PayHotAgreement, aid)
        old.status = 'CANCELLED'
        await sessions[0].commit()

        async def create(db, index):
            db.add(
                PayHotAgreement(
                    user_id=1,
                    owner_id=1,
                    subscription_id=1,
                    checkout_id=f'new-checkout-{index}',
                    status='ACTIVE',
                    consent_snapshot={},
                )
            )
            try:
                await db.commit()
                return True
            except IntegrityError as error:
                assert error.orig.__cause__.constraint_name == 'uq_payhot_agreement_live_target'
                await db.rollback()
                return False

        results = await asyncio.gather(create(sessions[1], 1), create(sessions[2], 2))
        assert sorted(results) == [False, True]
        rows = list((await sessions[0].scalars(select(PayHotAgreement))).all())
        assert len(rows) == 2 and sum(row.status == 'ACTIVE' for row in rows) == 1
