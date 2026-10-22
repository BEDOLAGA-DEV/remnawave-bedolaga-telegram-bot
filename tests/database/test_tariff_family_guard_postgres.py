"""Реальные PostgreSQL-инварианты семейств: INSERT, UPDATE и конкурентные покупки."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.database.models import Subscription, SubscriptionStatus, Tariff, User
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
