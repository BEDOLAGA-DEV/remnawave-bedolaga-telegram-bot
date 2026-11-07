"""Реальные PostgreSQL-инварианты семейств: INSERT, UPDATE и конкурентные покупки."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from app.database.crud.subscription import create_paid_subscription
from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.models import (
    PaymentMethod,
    Subscription,
    SubscriptionStatus,
    Tariff,
    Transaction,
    TransactionType,
    User,
)
from tests.database.test_retired_gateway_migration_postgres import _load
from tests.fixtures.postgres_db import lock_waiter_appeared, postgres_sessions


pytestmark = pytest.mark.postgres

TABLES = [Subscription.__table__, Tariff.__table__, User.__table__]


async def _fixtures(db):
    user = User(telegram_id=785001, first_name='Тест', language='ru', balance_kopeks=100000)
    basic = Tariff(name='Basic', entitlement_family='main')
    premium = Tariff(name='Premium', entitlement_family='main')
    addon = Tariff(name='CDN', entitlement_family='metered_bypass')
    db.add_all([user, basic, premium, addon])
    await db.commit()
    return user, basic, premium, addon


def _sub(user_id: int, tariff_id: int, short_id: str, status: str = 'active') -> Subscription:
    return Subscription(
        user_id=user_id,
        tariff_id=tariff_id,
        remnawave_short_id=short_id,
        is_trial=status == SubscriptionStatus.TRIAL.value,
        status=status,
        start_date=datetime.now(UTC),
        end_date=datetime.now(UTC) + timedelta(days=30),
    )


async def test_one_main_and_separate_addon_allowed_but_second_main_denied(postgres_database):
    async with postgres_sessions(postgres_database, TABLES, count=2) as (first, second):
        user, basic, premium, addon = await _fixtures(first)
        first.add_all([_sub(user.id, basic.id, 'family_main'), _sub(user.id, addon.id, 'family_cdn')])
        await first.commit()

        second.add(_sub(user.id, premium.id, 'family_other'))
        with pytest.raises(IntegrityError, match='active entitlement family conflict'):
            await second.flush()
        await second.rollback()

        existing = (await second.scalars(select(Subscription).where(Subscription.user_id == user.id))).all()
        assert {s.tariff_id for s in existing} == {basic.id, addon.id}


async def test_trial_and_limited_are_live_but_disabled_is_not(postgres_database):
    async with postgres_sessions(postgres_database, TABLES, count=2) as (first, second):
        user, basic, premium, _ = await _fixtures(first)
        first.add(_sub(user.id, basic.id, 'trial_main', 'trial'))
        await first.commit()
        second.add(_sub(user.id, premium.id, 'second_main'))
        with pytest.raises(IntegrityError):
            await second.flush()
        await second.rollback()

        trial = (await first.scalars(select(Subscription).where(Subscription.user_id == user.id))).one()
        trial.status = 'disabled'
        await first.commit()
        second.add(_sub(user.id, premium.id, 'second_main'))
        await second.commit()

        second_sub = (await first.scalars(select(Subscription).where(Subscription.tariff_id == premium.id))).one()
        second_sub.status = 'limited'
        await first.commit()
        trial.status = 'active'
        with pytest.raises(IntegrityError):
            await first.commit()
        await first.rollback()


async def test_family_change_cannot_make_two_live_subscriptions_conflict(postgres_database):
    async with postgres_sessions(postgres_database, TABLES, count=2) as (first, second):
        user, basic, premium, _ = await _fixtures(first)
        premium.entitlement_family = None
        await first.commit()
        first.add_all([_sub(user.id, basic.id, 'basic_alive'), _sub(user.id, premium.id, 'legacy_alive')])
        await first.commit()

        premium_id = premium.id
        premium.entitlement_family = 'main'
        with pytest.raises(IntegrityError, match='active entitlement family conflict'):
            await first.commit()
        await first.rollback()
        stored = await second.scalar(select(Tariff.entitlement_family).where(Tariff.id == premium_id))
        assert stored is None


async def test_two_simultaneous_main_inserts_are_serialized(postgres_database):
    async with postgres_sessions(postgres_database, TABLES, count=3) as (first, second, watcher):
        user, basic, premium, _ = await _fixtures(first)

        first.add(_sub(user.id, basic.id, 'first_main'))
        await first.flush()  # Транзакция ещё держит advisory lock.

        async def compete():
            second.add(_sub(user.id, premium.id, 'second_main'))
            try:
                await second.flush()
                return True
            except IntegrityError:
                await second.rollback()
                return False

        competing = asyncio.create_task(compete())
        locked = await lock_waiter_appeared(watcher)
        await first.commit()
        accepted_second = await asyncio.wait_for(competing, timeout=10)

        count = (
            await watcher.execute(
                text("SELECT count(*) FROM subscriptions WHERE user_id = :user_id AND status = 'active'"),
                {'user_id': user.id},
            )
        ).scalar_one()
        assert count == 1, 'конкурентные покупки создали два Main'
        assert accepted_second is False, 'вторая покупка прошла без семейного отказа'
        assert locked, 'PostgreSQL не наблюдал ожидания семейной блокировки'


async def test_family_conflict_undoes_wallet_charge_in_same_transaction(postgres_database):
    """Триггер не должен оставлять списание без продукта при общем COMMIT."""
    async with postgres_sessions(postgres_database, TABLES, count=2) as (first, second):
        user, basic, premium, _ = await _fixtures(first)
        user_id, basic_id, premium_id = user.id, basic.id, premium.id
        first.add(_sub(user_id, basic_id, 'wallet_main'))
        await first.commit()

        original_balance = await second.scalar(select(User.balance_kopeks).where(User.id == user_id))
        await first.execute(update(User).where(User.id == user_id).values(balance_kopeks=User.balance_kopeks - 250))
        first.add(_sub(user_id, premium_id, 'wallet_conflict'))
        with pytest.raises(IntegrityError, match='active entitlement family conflict'):
            await first.flush()
        await first.rollback()

        stored_balance = await second.scalar(select(User.balance_kopeks).where(User.id == user_id))
        stored_subscriptions = (await second.scalars(select(Subscription).where(Subscription.user_id == user_id))).all()
        assert stored_balance == original_balance
        assert [item.tariff_id for item in stored_subscriptions] == [basic_id]


async def test_guard_migration_0142_installs_xor_on_existing_database(postgres_database):
    """Upgrade 0142 must enforce Main XOR, not only Base.metadata.create_all."""
    migration = _load('0142_guard_active_entitlement_family.py')
    async with postgres_sessions(postgres_database, TABLES, count=2) as (first, second):
        user, basic, premium, _ = await _fixtures(first)
        user_id, basic_id, premium_id = user.id, basic.id, premium.id
        first.add(_sub(user_id, basic_id, 'migrated_main'))
        await first.commit()

        connection = await first.connection()

        def reinstall(sync):
            with Operations.context(MigrationContext.configure(sync)):
                migration.downgrade()
                migration.upgrade()

        await connection.run_sync(reinstall)
        await first.commit()

        second.add(_sub(user_id, premium_id, 'migrated_second'))
        with pytest.raises(IntegrityError, match='active entitlement family conflict'):
            await second.flush()
        await second.rollback()


async def test_real_balance_crud_and_subscription_creation_roll_back_together(postgres_database):
    """Списанные деньги и финансовая запись должны исчезнуть после отказа XOR.

    В отличие от SQL-only теста проверяет реальные CRUD-примитивы покупки с
    commit=False на выделенной PostgreSQL-транзакции.
    """
    async with postgres_sessions(postgres_database, TABLES + [Transaction.__table__], count=2) as (first, second):
        user, basic, premium, _ = await _fixtures(first)
        user_id, basic_id, premium_id = user.id, basic.id, premium.id
        first.add(_sub(user_id, basic_id, 'existing_main'))
        await first.commit()

        original_balance = await second.scalar(select(User.balance_kopeks).where(User.id == user_id))
        assert await subtract_user_balance(
            first, user, 250, 'Покупка Main', mark_as_paid_subscription=True, commit=False
        )
        await create_transaction(
            first,
            user_id=user_id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=250,
            description='Покупка Main',
            payment_method=PaymentMethod.BALANCE,
            commit=False,
        )
        with pytest.raises(IntegrityError, match='active entitlement family conflict'):
            await create_paid_subscription(
                first,
                user_id=user_id,
                duration_days=30,
                tariff_id=premium_id,
                connected_squads=[],
                commit=False,
            )
        await first.rollback()

        assert await second.scalar(select(User.balance_kopeks).where(User.id == user_id)) == original_balance
        assert await second.scalar(select(Transaction.id).where(Transaction.user_id == user_id)) is None
        ids = (await second.scalars(select(Subscription.tariff_id).where(Subscription.user_id == user_id))).all()
        assert ids == [basic_id]
