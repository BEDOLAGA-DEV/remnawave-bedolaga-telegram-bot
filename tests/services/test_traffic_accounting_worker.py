"""Bounded accounting recovery survives restarts without hammering the panel."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database.models import TrafficAccountingState
from tests.services.test_traffic_accounting_store import database, initialize


NOW = datetime(2026, 1, 1, tzinfo=UTC)


class PanelService:
    @asynccontextmanager
    async def get_api_client(self):
        yield object()


async def test_failure_backoff_is_persisted_and_survives_new_worker(monkeypatch):
    from app.services.traffic_accounting_worker import TrafficAccountingWorker

    async with database(monkeypatch) as db:
        state = await initialize(db)
        state.phase = 'ready'
        await db.commit()
        deliver = AsyncMock(side_effect=TimeoutError('secret provider URL'))
        monkeypatch.setattr('app.services.traffic_accounting_worker.deliver_account_projection', deliver)
        factory = async_sessionmaker(db.bind, expire_on_commit=False)
        worker = TrafficAccountingWorker(factory, PanelService)
        assert await worker.run_once(now=NOW) == 1
        await db.refresh(state)
        assert state.delivery_failures == 1
        assert state.last_delivery_error == 'TimeoutError'
        assert state.next_delivery_at == NOW + timedelta(seconds=30)
        restarted = TrafficAccountingWorker(factory, PanelService)
        assert await restarted.run_once(now=NOW + timedelta(seconds=29)) == 0
        assert await restarted.run_once(now=NOW + timedelta(seconds=30)) == 1
        await db.refresh(state)
        assert state.delivery_failures == 2
        assert state.next_delivery_at == NOW + timedelta(seconds=90)
        assert deliver.await_count == 2


async def test_prepared_recovers_then_polls_without_hot_loop(monkeypatch):
    from app.services.traffic_accounting_worker import TrafficAccountingWorker

    async with database(monkeypatch) as db:
        state = await initialize(db)
        await db.commit()
        factory = async_sessionmaker(db.bind, expire_on_commit=False)

        async def migrate(factory, api, subscription_id, **kwargs):
            async with factory() as session:
                row = await session.get(TrafficAccountingState, subscription_id)
                row.phase = 'ready'
                row.projected_revision = row.revision
                await session.commit()

        migrate_mock = AsyncMock(side_effect=migrate)
        monkeypatch.setattr('app.services.traffic_accounting_worker.ensure_managed_account', migrate_mock)
        monkeypatch.setattr('app.services.traffic_accounting_worker.deliver_account_projection', AsyncMock())
        worker = TrafficAccountingWorker(factory, PanelService)
        assert await worker.run_once(now=NOW) == 1
        migrate_mock.assert_awaited_once()
        assert await worker.run_once(now=NOW + timedelta(seconds=1)) == 0
        assert await worker.run_once(now=NOW + timedelta(seconds=60)) == 1
        await db.refresh(state)
        assert state.delivery_failures == 0


async def test_stop_cancels_inflight_delivery_and_closes_client(monkeypatch):
    from app.services.traffic_accounting_worker import TrafficAccountingWorker

    entered, closed = asyncio.Event(), asyncio.Event()

    class Service:
        @asynccontextmanager
        async def get_api_client(self):
            try:
                yield object()
            finally:
                closed.set()

    async def deliver(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    async with database(monkeypatch) as db:
        state = await initialize(db)
        state.phase = 'ready'
        await db.commit()
        worker = TrafficAccountingWorker(async_sessionmaker(db.bind, expire_on_commit=False), Service)
        monkeypatch.setattr('app.services.traffic_accounting_worker.deliver_account_projection', deliver)
        await worker.start()
        try:
            await asyncio.wait_for(entered.wait(), 2)
        finally:
            await worker.stop()
        assert closed.is_set()
        assert worker.task is None


async def test_expiry_wakes_early_but_grace_uses_normal_poll(monkeypatch):
    from app.services.traffic_accounting_worker import TrafficAccountingWorker

    async with database(monkeypatch) as db:
        state = await initialize(db)
        state.phase = 'ready'
        state.projected_revision = state.revision
        state.next_refresh_at = NOW + timedelta(seconds=10)
        await db.commit()
        factory = async_sessionmaker(db.bind, expire_on_commit=False)
        monkeypatch.setattr('app.services.traffic_accounting_worker.deliver_account_projection', AsyncMock())
        worker = TrafficAccountingWorker(factory, PanelService)
        assert await worker.run_once(now=NOW) == 1
        await db.refresh(state)
        assert state.next_delivery_at == NOW + timedelta(seconds=10)
        state.grace_excluded = True
        state.next_delivery_at = None
        await db.commit()
        assert await worker.run_once(now=NOW) == 1
        await db.refresh(state)
        assert state.next_delivery_at == NOW + timedelta(seconds=60)
        assert await worker.run_once(now=NOW + timedelta(seconds=11)) == 0


async def test_timeout_cancels_panel_call_and_records_retry(monkeypatch):
    from app.services.traffic_accounting_worker import TrafficAccountingWorker

    cancelled = asyncio.Event()

    async def deliver(*args, **kwargs):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async with database(monkeypatch) as db:
        state = await initialize(db)
        state.phase = 'ready'
        await db.commit()
        worker = TrafficAccountingWorker(
            async_sessionmaker(db.bind, expire_on_commit=False), PanelService, request_timeout=0.01
        )
        monkeypatch.setattr('app.services.traffic_accounting_worker.deliver_account_projection', deliver)
        assert await worker.run_once(now=NOW) == 1
        await db.refresh(state)
        assert cancelled.is_set()
        assert state.last_delivery_error == 'TimeoutError'
        assert state.next_delivery_at == NOW + timedelta(seconds=30)


async def test_batch_fairness_advances_past_failed_first_account(monkeypatch):
    from app.database.models import Subscription
    from app.services.traffic_accounting import Allowance, TrafficAccount
    from app.services.traffic_accounting_store import initialize_account
    from app.services.traffic_accounting_worker import TrafficAccountingWorker

    async with database(monkeypatch) as db:
        state = await initialize(db)
        state.phase = 'ready'
        for sid in (2, 3):
            db.add(
                Subscription(
                    id=sid,
                    user_id=1,
                    is_trial=False,
                    start_date=NOW,
                    end_date=NOW + timedelta(days=30),
                    remnawave_short_id=f'masked-{sid}',
                )
            )
        await db.flush()
        for sid in (2, 3):
            row = await initialize_account(
                db,
                sid,
                TrafficAccount(str(sid), 0, 0, (Allowance('base:test', 100, NOW + timedelta(days=30)),)),
                strategy='MONTH',
                timezone='UTC',
                anchor=NOW,
                next_refresh_at=None,
            )
            row.phase = 'ready'
        await db.commit()
        deliver = AsyncMock(side_effect=RuntimeError('offline'))
        monkeypatch.setattr('app.services.traffic_accounting_worker.deliver_account_projection', deliver)
        worker = TrafficAccountingWorker(
            async_sessionmaker(db.bind, expire_on_commit=False), PanelService, batch_size=1
        )
        assert await worker.run_once(now=NOW) == 1
        assert await worker.run_once(now=NOW) == 1
        assert await worker.run_once(now=NOW) == 1
        assert {call.args[2] for call in deliver.await_args_list} == {1, 2, 3}
        assert await worker.run_once(now=NOW) == 0


async def test_legacy_failure_cools_down_without_state_or_money_mutation(monkeypatch):
    from app.database.models import Subscription, TrafficPurchase, User
    from app.services.traffic_accounting_worker import TrafficAccountingWorker

    async with database(monkeypatch) as db:
        sub = await db.get(Subscription, 1)
        sub.traffic_limit_gb = 10
        sub.traffic_accounting_mode = 'legacy'
        sub.grace_session_open = False
        user = await db.get(User, 1)
        user.status = 'active'
        db.add(TrafficPurchase(subscription_id=1, traffic_gb=5, expires_at=NOW + timedelta(days=10)))
        await db.commit()
        migrate = AsyncMock(side_effect=ConnectionError('credentials must never reach logs'))
        monkeypatch.setattr('app.services.traffic_accounting_worker.ensure_managed_account', migrate)
        worker = TrafficAccountingWorker(async_sessionmaker(db.bind, expire_on_commit=False), PanelService)
        assert await worker.run_once(now=NOW) == 1
        assert await worker.run_once(now=NOW + timedelta(seconds=1)) == 0
        assert await worker.run_once(now=NOW + timedelta(seconds=30)) == 1
        assert migrate.await_count == 2
        assert await db.get(TrafficAccountingState, 1) is None
        await db.refresh(sub)
        assert sub.traffic_limit_gb == 10


async def test_only_explicit_missing_account_starts_recreation(monkeypatch):
    from app.services.traffic_accounting_service import AccountingAccountMissing
    from app.services.traffic_accounting_worker import TrafficAccountingWorker

    async with database(monkeypatch) as db:
        state = await initialize(db)
        state.phase = 'ready'
        await db.commit()
        deliver = AsyncMock(side_effect=[AccountingAccountMissing('missing'), object()])
        recreate = AsyncMock()
        monkeypatch.setattr('app.services.traffic_accounting_worker.deliver_account_projection', deliver)
        monkeypatch.setattr('app.services.traffic_accounting_recreation.ensure_account_recreated', recreate)
        worker = TrafficAccountingWorker(async_sessionmaker(db.bind, expire_on_commit=False), PanelService)
        assert await worker.run_once(now=NOW) == 1
        recreate.assert_awaited_once()
        assert deliver.await_count == 2
        deliver.side_effect = RuntimeError('transport unavailable')
        assert await worker.run_once(now=NOW + timedelta(seconds=61)) == 1
        recreate.assert_awaited_once()


async def test_stop_drains_immediate_projections_even_without_worker_task(monkeypatch):
    from app.services.traffic_accounting_worker import TrafficAccountingWorker

    drain = AsyncMock()
    monkeypatch.setattr('app.services.traffic_accounting_service.stop_projection_tasks', drain)
    worker = TrafficAccountingWorker()
    await worker.stop()
    drain.assert_awaited_once_with(timeout=5)


async def _seed_many(db, count):
    from app.database.models import Subscription

    for sid in range(2, count + 1):
        db.add(
            Subscription(
                id=sid, user_id=1, start_date=NOW, end_date=NOW + timedelta(days=30), remnawave_short_id=f'masked-{sid}'
            )
        )
    await db.flush()
    for sid in range(1, count + 1):
        db.add(
            TrafficAccountingState(
                subscription_id=sid,
                panel_identity=str(sid),
                lifetime_bytes=0,
                used_bytes=0,
                revision=0,
                projected_revision=0,
                phase='ready',
                business_strategy='NO_RESET',
                calendar_timezone='UTC',
                calendar_anchor=NOW,
                next_delivery_at=NOW - timedelta(hours=1),
            )
        )
    await db.commit()


async def test_due_calendar_expiry_and_delivery_precede_old_routine_polls(monkeypatch):
    from app.database.models import TrafficAllowance
    from app.services.traffic_accounting_worker import TrafficAccountingWorker

    async with database(monkeypatch) as db:
        await _seed_many(db, 103)
        for sid in (101, 102, 103):
            state = await db.get(TrafficAccountingState, sid)
            state.next_delivery_at = NOW
        (await db.get(TrafficAccountingState, 101)).next_refresh_at = NOW
        db.add(
            TrafficAllowance(
                subscription_id=102, operation_key='purchase:expiry', granted_bytes=1, remaining_bytes=1, expires_at=NOW
            )
        )
        (await db.get(TrafficAccountingState, 103)).revision = 1
        await db.commit()
        deliver = AsyncMock()
        monkeypatch.setattr('app.services.traffic_accounting_worker.deliver_account_projection', deliver)
        worker = TrafficAccountingWorker(
            async_sessionmaker(db.bind, expire_on_commit=False), PanelService, batch_size=3
        )
        worker._record_result = AsyncMock()
        assert await worker.run_once(now=NOW) == 3
        assert {call.args[2] for call in deliver.await_args_list} == {101, 102, 103}


async def test_parallel_delivery_is_bounded_and_stop_closes_every_client(monkeypatch):
    from app.services.traffic_accounting_worker import TrafficAccountingWorker

    active = 0
    maximum = 0
    all_entered = asyncio.Event()
    closed = 0

    class Service:
        @asynccontextmanager
        async def get_api_client(self):
            nonlocal active, maximum, closed
            active += 1
            maximum = max(maximum, active)
            if active == 4:
                all_entered.set()
            try:
                yield object()
            finally:
                active -= 1
                closed += 1

    async def deliver(*args, **kwargs):
        await asyncio.Future()

    async with database(monkeypatch) as db:
        await _seed_many(db, 12)
        worker = TrafficAccountingWorker(async_sessionmaker(db.bind, expire_on_commit=False), Service)
        monkeypatch.setattr('app.services.traffic_accounting_worker.deliver_account_projection', deliver)
        worker._record_result = AsyncMock()
        await worker.start()
        try:
            await asyncio.wait_for(all_entered.wait(), 2)
            assert maximum == 4
        finally:
            await worker.stop()
        assert active == 0
        assert closed == 4
        assert worker.task is None


async def test_full_batches_drain_without_idle_delay(monkeypatch):
    from app.services import traffic_accounting_worker as module

    worker = module.TrafficAccountingWorker()
    worker.run_once = AsyncMock(side_effect=[worker.batch_size, 0])
    pauses = []

    async def pause(seconds):
        pauses.append(seconds)
        if len(pauses) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(module.asyncio, 'sleep', pause)
    import pytest

    with pytest.raises(asyncio.CancelledError):
        await worker._run()
    assert pauses == [worker.drain_pause_seconds, worker.interval_seconds]
    assert 0 < pauses[0] < pauses[1]


async def test_priority_queue_keeps_a_bounded_share_for_legacy_migration(monkeypatch):
    from app.database.models import Subscription, TrafficPurchase, User
    from app.services.traffic_accounting_worker import TrafficAccountingWorker

    async with database(monkeypatch) as db:
        await _seed_many(db, 5)
        for sid in range(1, 5):
            (await db.get(TrafficAccountingState, sid)).next_refresh_at = NOW
        await db.delete(await db.get(TrafficAccountingState, 5))
        legacy = await db.get(Subscription, 5)
        legacy.traffic_limit_gb = 10
        legacy.traffic_accounting_mode = 'legacy'
        (await db.get(User, 1)).status = 'active'
        db.add(TrafficPurchase(subscription_id=5, traffic_gb=5, expires_at=NOW + timedelta(days=10)))
        await db.commit()
        migrate = AsyncMock()
        deliver = AsyncMock()
        monkeypatch.setattr('app.services.traffic_accounting_worker.ensure_managed_account', migrate)
        monkeypatch.setattr('app.services.traffic_accounting_worker.deliver_account_projection', deliver)
        worker = TrafficAccountingWorker(
            async_sessionmaker(db.bind, expire_on_commit=False), PanelService, batch_size=5
        )
        worker._record_result = AsyncMock()
        assert await worker.run_once(now=NOW) == 5
        assert migrate.await_count == 1
        assert migrate.await_args.args[2] == 5
        assert {call.args[2] for call in deliver.await_args_list} == set(range(1, 6))
