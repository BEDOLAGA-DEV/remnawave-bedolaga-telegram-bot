"""Expiration workers must preserve concurrently purchased traffic packages."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select, text

from app.config import settings
from app.database.crud.subscription import add_subscription_traffic
from app.database.models import Base, Subscription, Tariff, TrafficPurchase, User
from app.services.daily_subscription_service import DailySubscriptionService
from tests.fixtures.postgres_db import lock_waiter_appeared, postgres_sessions


pytestmark = pytest.mark.postgres
TABLES = list(Base.metadata.sorted_tables)


@pytest.fixture(autouse=True)
def isolate_external_effects(monkeypatch):
    from app.services import subscription_service

    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    panel = SimpleNamespace(
        update_remnawave_user=AsyncMock(return_value=SimpleNamespace(used_traffic_bytes=0)),
        _gb_to_bytes=lambda gb: int(gb * 1024**3),
    )
    monkeypatch.setattr(subscription_service, 'SubscriptionService', lambda: panel)
    monkeypatch.setattr('app.services.recurrent_amount.sync_recurrent_bindings_after_price_change', AsyncMock())


async def seed(db):
    now = datetime.now(UTC)
    user = User(id=1, status='active', balance_kopeks=10000)
    tariff = Tariff(id=1, name='Test', traffic_limit_gb=100, traffic_reset_mode='NO_RESET')
    sub = Subscription(
        id=1,
        user_id=1,
        tariff_id=1,
        status='active',
        is_trial=False,
        start_date=now - timedelta(days=31),
        end_date=now + timedelta(days=30),
        traffic_limit_gb=175,
        purchased_traffic_gb=75,
        traffic_used_gb=10,
        traffic_reset_at=now - timedelta(hours=1),
        remnawave_id=123,
        remnawave_short_id='test-one',
    )
    expired = TrafficPurchase(subscription_id=1, traffic_gb=50, expires_at=now - timedelta(hours=1))
    active = TrafficPurchase(subscription_id=1, traffic_gb=25, expires_at=now + timedelta(days=10))
    db.add_all([user, tariff, sub, expired, active])
    await db.commit()
    return sub, expired


async def test_stale_second_worker_does_not_remove_active_package(postgres_database):
    async with postgres_sessions(postgres_database, TABLES, count=2) as (first, second):
        sub, expired = await seed(first)
        sid, pid = sub.id, expired.id
        stale = await second.get(TrafficPurchase, expired.id)
        stale_sub = await second.get(Subscription, sub.id)
        assert stale_sub.traffic_limit_gb == 175
        service = DailySubscriptionService()
        await service._reset_subscription_traffic(first, sid, [pid])
        await first.commit()
        await service._reset_subscription_traffic(second, sid, [stale.id])
        await second.commit()
        await first.refresh(sub)
        assert sub.traffic_limit_gb == 125
        assert sub.purchased_traffic_gb == 25
        assert [p.traffic_gb for p in await first.scalars(select(TrafficPurchase))] == [25]


async def test_expiration_cannot_overwrite_concurrent_topup(postgres_database, monkeypatch):
    async with postgres_sessions(postgres_database, TABLES, count=3) as (expiry_db, buyer_db, observer):
        sub, expired = await seed(expiry_db)
        sid = sub.id
        service = DailySubscriptionService()
        calculated, proceed = asyncio.Event(), asyncio.Event()
        original = service._should_defer_limit_drop

        async def pause_after_calculation(*args):
            calculated.set()
            await asyncio.wait_for(proceed.wait(), 10)
            return await original(*args)

        monkeypatch.setattr(service, '_should_defer_limit_drop', pause_after_calculation)
        expiry_task = asyncio.create_task(service._reset_subscription_traffic(expiry_db, sid, [expired.id]))
        buyer_task = None
        try:
            await asyncio.wait_for(calculated.wait(), 5)
            buyer_sub = await buyer_db.get(Subscription, sid)
            buyer_task = asyncio.create_task(add_subscription_traffic(buyer_db, buyer_sub, 40))
            buyer_waited = await lock_waiter_appeared(observer, timeout=1)
        finally:
            proceed.set()
            await asyncio.wait_for(asyncio.gather(*([expiry_task, buyer_task] if buyer_task else [expiry_task])), 10)
        await observer.refresh(await observer.get(Subscription, sid))
        fresh = await observer.get(Subscription, sid)
        assert fresh.traffic_limit_gb == 165
        assert fresh.purchased_traffic_gb == 65
        assert sum(p.traffic_gb for p in await observer.scalars(select(TrafficPurchase))) == 65
        assert buyer_waited, 'The buyer must actually overlap the locked expiration'


async def test_rescheduled_candidate_is_not_expired_from_stale_snapshot(postgres_database):
    async with postgres_sessions(postgres_database, TABLES, count=2) as (worker_db, admin_db):
        sub, expired = await seed(worker_db)
        current = await admin_db.get(TrafficPurchase, expired.id)
        current.expires_at = datetime.now(UTC) + timedelta(days=5)
        await admin_db.commit()
        await DailySubscriptionService()._reset_subscription_traffic(worker_db, sub.id, [expired.id])
        await worker_db.refresh(sub)
        assert sub.traffic_limit_gb == 175
        assert sub.purchased_traffic_gb == 75
        assert len(list(await worker_db.scalars(select(TrafficPurchase)))) == 2


async def test_deferred_expiration_releases_lock_without_changing_packages(postgres_database, monkeypatch):
    async with postgres_sessions(postgres_database, TABLES, count=1) as (db,):
        sub, expired = await seed(db)
        tariff = await db.get(Tariff, sub.tariff_id)
        tariff.traffic_reset_mode = 'MONTH'
        await db.commit()
        service = DailySubscriptionService()
        monkeypatch.setattr(service, '_get_panel_used_gb', AsyncMock(return_value=150))
        assert await service._reset_subscription_traffic(db, sub.id, [expired.id]) == 0
        assert not db.in_transaction()
        await db.refresh(sub)
        assert sub.traffic_limit_gb == 175
        assert len(list(await db.scalars(select(TrafficPurchase)))) == 2


async def test_cancelled_expiration_releases_lock(postgres_database, monkeypatch):
    async with postgres_sessions(postgres_database, TABLES, count=1) as (db,):
        sub, expired = await seed(db)
        service = DailySubscriptionService()
        waiting = asyncio.Event()

        async def interrupt(*args):
            waiting.set()
            await asyncio.Future()

        monkeypatch.setattr(service, '_should_defer_limit_drop', interrupt)
        task = asyncio.create_task(service._reset_subscription_traffic(db, sub.id, [expired.id]))
        await asyncio.wait_for(waiting.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not db.in_transaction()
        await db.refresh(sub)
        assert sub.traffic_limit_gb == 175
        assert len(list(await db.scalars(select(TrafficPurchase)))) == 2


async def test_failed_subscription_does_not_abort_remaining_batch(postgres_database, monkeypatch):
    from contextlib import asynccontextmanager

    from app.services import daily_subscription_service as module

    async with postgres_sessions(postgres_database, TABLES, count=1) as (db,):
        sub, _ = await seed(db)
        now = datetime.now(UTC)
        db.add(Tariff(id=2, name='Other', traffic_limit_gb=100, traffic_reset_mode='NO_RESET'))
        other = Subscription(
            id=2,
            user_id=1,
            tariff_id=2,
            status='active',
            is_trial=False,
            start_date=now - timedelta(days=31),
            end_date=now + timedelta(days=30),
            traffic_limit_gb=125,
            purchased_traffic_gb=25,
            traffic_used_gb=10,
            remnawave_id=456,
            remnawave_short_id='test-two',
        )
        db.add(other)
        await db.flush()
        db.add(TrafficPurchase(subscription_id=2, traffic_gb=25, expires_at=now - timedelta(hours=1)))
        await db.commit()

        @asynccontextmanager
        async def session():
            yield db

        monkeypatch.setattr(module, 'AsyncSessionLocal', session)
        service = DailySubscriptionService()
        original = service._should_defer_limit_drop

        async def fail_one(db, subscription, *args):
            if subscription.id == 1:
                await db.execute(text('SELECT 1 / 0'))  # real aborted PostgreSQL transaction
            return await original(db, subscription, *args)

        monkeypatch.setattr(service, '_should_defer_limit_drop', fail_one)
        stats = await service.process_traffic_resets()
        assert stats == {'checked': 2, 'reset': 1, 'errors': 1}
        await db.refresh(sub)
        await db.refresh(other)
        assert sub.traffic_limit_gb == 175
        assert other.traffic_limit_gb == 100
        assert other.purchased_traffic_gb == 0


async def test_expiration_refreshes_cached_totals_after_completed_topup(postgres_database):
    async with postgres_sessions(postgres_database, TABLES, count=2) as (worker_db, buyer_db):
        stale_sub, expired = await seed(worker_db)
        sid, pid = stale_sub.id, expired.id
        buyer_sub = await buyer_db.get(Subscription, sid)
        await add_subscription_traffic(buyer_db, buyer_sub, 40)
        assert stale_sub.traffic_limit_gb == 175  # deliberately retained stale ORM instance
        await DailySubscriptionService()._reset_subscription_traffic(worker_db, sid, [pid])
        await worker_db.refresh(stale_sub)
        assert stale_sub.traffic_limit_gb == 165
        assert stale_sub.purchased_traffic_gb == 65


@pytest.mark.parametrize('outcome', ['none', 'raise', 'reset-none', 'db-error', 'local-commit-error'])
async def test_panel_rollback_keeps_expiry_and_enqueues_retry(postgres_database, monkeypatch, outcome):
    from app.services import remnawave_retry_queue as retry, subscription_service

    queue = retry.RemnaWaveRetryQueue()
    monkeypatch.setattr(retry, 'remnawave_retry_queue', queue)
    enqueue = Mock(wraps=queue.enqueue)
    monkeypatch.setattr(queue, 'enqueue', enqueue)

    calls = []

    async def unavailable(db, subscription, **kwargs):
        calls.append(kwargs)
        if outcome in {'reset-none', 'local-commit-error'} and len(calls) == 1:
            return SimpleNamespace(used_traffic_bytes=200 * 1024**3)
        if outcome == 'local-commit-error':
            return SimpleNamespace(used_traffic_bytes=0)
        if outcome == 'db-error':
            await db.execute(text('SELECT 1 / 0'))
        await db.execute(select(Subscription.id))  # open a real transaction before rollback
        await db.rollback()
        if outcome == 'raise':
            raise RuntimeError('synthetic panel failure')
        return None

    monkeypatch.setattr(
        subscription_service,
        'SubscriptionService',
        lambda: SimpleNamespace(update_remnawave_user=unavailable, _gb_to_bytes=lambda gb: int(gb * 1024**3)),
    )
    async with postgres_sessions(postgres_database, TABLES, count=1) as (db,):
        sub, expired = await seed(db)
        if outcome == 'local-commit-error':
            original_commit = db.commit
            commits = 0

            async def fail_second_commit():
                nonlocal commits
                commits += 1
                if commits == 2:
                    await db.execute(text('SELECT 1 / 0'))
                await original_commit()

            monkeypatch.setattr(db, 'commit', fail_second_commit)
        assert await DailySubscriptionService()._reset_subscription_traffic(db, sub.id, [expired.id]) == 1
        await db.refresh(sub)
        assert sub.traffic_limit_gb == 125
        assert sub.purchased_traffic_gb == 25
        assert len(list(await db.scalars(select(TrafficPurchase)))) == 1
        assert queue.pending_count == 1
        assert sub.traffic_used_gb == 10
        if outcome == 'reset-none':
            assert calls[1]['reset_traffic'] is True
        assert enqueue.call_args.kwargs['reset_traffic'] is (outcome == 'reset-none')
