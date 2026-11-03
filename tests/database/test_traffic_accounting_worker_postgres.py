"""Exercise the recovery scheduler's persisted fences against PostgreSQL."""

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from app.database.models import Base, Subscription, User
from tests.fixtures.postgres_db import postgres_sessions
from tests.services import test_traffic_accounting_worker as checks


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    'check',
    [
        checks.test_failure_backoff_is_persisted_and_survives_new_worker,
        checks.test_prepared_recovers_then_polls_without_hot_loop,
        checks.test_stop_cancels_inflight_delivery_and_closes_client,
        checks.test_expiry_wakes_early_but_grace_uses_normal_poll,
        checks.test_timeout_cancels_panel_call_and_records_retry,
        checks.test_batch_fairness_advances_past_failed_first_account,
        checks.test_legacy_failure_cools_down_without_state_or_money_mutation,
        checks.test_only_explicit_missing_account_starts_recreation,
        checks.test_due_calendar_expiry_and_delivery_precede_old_routine_polls,
        checks.test_parallel_delivery_is_bounded_and_stop_closes_every_client,
    ],
    ids=lambda check: check.__name__.removeprefix('test_'),
)
async def test_worker_recovery_on_postgres(postgres_database, monkeypatch, check):
    @asynccontextmanager
    async def database(_monkeypatch):
        async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=1) as sessions:
            db = sessions[0]
            db.add(User(id=1))
            db.add(Subscription(id=1, user_id=1, start_date=checks.NOW, end_date=checks.NOW + timedelta(days=30)))
            await db.commit()
            yield db

    monkeypatch.setattr(checks, 'database', database)
    await check(monkeypatch)


@pytest.mark.parametrize('old_due', [None, checks.NOW + timedelta(minutes=30)])
async def test_new_paid_intent_wins_over_stale_worker_result(postgres_database, monkeypatch, old_due):
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.database.models import TrafficAccountingState
    from app.services.traffic_accounting_worker import TrafficAccountingWorker
    from app.services.traffic_base_grant_service import stage_paid_base_grant
    from tests.services.test_traffic_accounting_store import initialize

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=1) as sessions:
        db = sessions[0]
        db.add(User(id=1))
        db.add(
            Subscription(
                id=1,
                user_id=1,
                traffic_accounting_mode='managed',
                traffic_limit_gb=100,
                start_date=checks.NOW,
                end_date=checks.NOW + timedelta(days=30),
            )
        )
        await db.commit()
        state = await initialize(db)
        state.phase = 'ready'
        state.next_delivery_at = old_due
        original_revision = state.revision
        await db.commit()
        read, resume = asyncio.Event(), asyncio.Event()

        class StaleResultSession(AsyncSession):
            async def get(self, entity, ident, **kwargs):
                result = await super().get(entity, ident, **kwargs)
                if entity is TrafficAccountingState:
                    read.set()
                    await resume.wait()
                return result

        worker = TrafficAccountingWorker(async_sessionmaker(db.bind, class_=StaleResultSession, expire_on_commit=False))
        result_task = asyncio.create_task(worker._record_result(1, checks.NOW, 'TimeoutError'))
        try:
            await asyncio.wait_for(read.wait(), 5)
            factory = async_sessionmaker(db.bind, expire_on_commit=False)
            async with factory() as buyer:
                subscription = await buyer.get(Subscription, 1)
                await stage_paid_base_grant(buyer, subscription, operation_key='renewal:new-paid-period')
                await buyer.commit()
            resume.set()
            await asyncio.wait_for(result_task, 5)
        finally:
            resume.set()
            await asyncio.gather(result_task, return_exceptions=True)
        await db.refresh(state)
        assert state.next_delivery_at is None
        assert state.revision == original_revision + 1
        # Re-delivering the same payment must not keep waking the worker.
        sub = await db.get(Subscription, 1)
        await stage_paid_base_grant(db, sub, operation_key='renewal:new-paid-period')
        await db.commit()
        assert state.revision == original_revision + 1
