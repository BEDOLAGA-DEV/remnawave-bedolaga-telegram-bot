"""Opt-in local scheduler measurement: real DB, in-process fake panel, no network."""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from time import perf_counter
from types import SimpleNamespace

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.database.models import Base, Subscription, TrafficAccountingState, TrafficAllowance, User
from app.services.traffic_accounting_worker import TrafficAccountingWorker
from tests.fixtures.postgres_db import postgres_engine


pytestmark = pytest.mark.postgres


async def test_thousand_simultaneous_calendar_updates_with_fake_panel(postgres_database, monkeypatch):
    if os.environ.get('RUN_TRAFFIC_WORKER_LOAD') != '1':
        pytest.skip('Explicit local load measurement requested via RUN_TRAFFIC_WORKER_LOAD=1')
    count = 1000
    now = datetime(2026, 11, 2, 0, 5, tzinfo=UTC)
    created = now - timedelta(days=30)
    end = now + timedelta(days=30)
    gb = 1024**3
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr('app.services.traffic_accounting_service.schedule_projection_after_commit', lambda *a: None)
    calls = {'get': 0, 'patch': 0, 'active': 0, 'maximum': 0}

    class FakePanel:
        async def get_user_by_id(self, panel_id):
            calls['get'] += 1
            return SimpleNamespace(
                id=panel_id,
                created_at=created,
                user_traffic=SimpleNamespace(used_traffic_bytes=100 * gb, lifetime_used_traffic_bytes=100 * gb),
                traffic_limit_bytes=100 * gb,
                traffic_limit_strategy='NO_RESET',
                status='ACTIVE',
                expire_at=end,
            )

        async def update_user(self, **kwargs):
            calls['patch'] += 1
            assert kwargs['traffic_limit_bytes'] == 200 * gb
            return SimpleNamespace(id=kwargs['user_id'])

    class Service:
        @asynccontextmanager
        async def get_api_client(self):
            calls['active'] += 1
            calls['maximum'] = max(calls['maximum'], calls['active'])
            try:
                yield FakePanel()
            finally:
                calls['active'] -= 1

    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            await db.execute(insert(User), [{'id': sid, 'status': 'active'} for sid in range(1, count + 1)])
            await db.execute(
                insert(Subscription),
                [
                    {
                        'id': sid,
                        'user_id': sid,
                        'status': 'active',
                        'is_trial': False,
                        'start_date': created,
                        'end_date': end,
                        'traffic_limit_gb': 100,
                        'traffic_accounting_mode': 'managed',
                        'remnawave_id': sid,
                        'remnawave_short_id': f'masked-{sid}',
                    }
                    for sid in range(1, count + 1)
                ],
            )
            await db.execute(
                insert(TrafficAccountingState),
                [
                    {
                        'subscription_id': sid,
                        'panel_identity': f'{sid}@{created.isoformat()}',
                        'lifetime_bytes': 100 * gb,
                        'used_bytes': 100 * gb,
                        'revision': 0,
                        'projected_revision': 0,
                        'phase': 'ready',
                        'business_strategy': 'DAY',
                        'calendar_timezone': 'UTC',
                        'calendar_anchor': created,
                        'next_refresh_at': now,
                        'next_delivery_at': now,
                        'base_budget_bytes': 100 * gb,
                    }
                    for sid in range(1, count + 1)
                ],
            )
            await db.execute(
                insert(TrafficAllowance),
                [
                    {
                        'subscription_id': sid,
                        'operation_key': 'base:old',
                        'granted_bytes': 100 * gb,
                        'remaining_bytes': 0,
                        'expires_at': now,
                    }
                    for sid in range(1, count + 1)
                ],
            )
            await db.commit()
        worker = TrafficAccountingWorker(factory, Service)
        start = perf_counter()
        processed = 0
        while batch := await worker.run_once(now=now):
            processed += batch
            assert processed <= count, 'Freshly delivered work must not loop immediately'
            if batch >= worker.batch_size:
                await asyncio.sleep(worker.drain_pause_seconds)
        duration = perf_counter() - start
        async with factory() as db:
            failed = await db.scalar(
                select(func.count())
                .select_from(TrafficAccountingState)
                .where(TrafficAccountingState.delivery_failures > 0)
            )
            pending = await db.scalar(
                select(func.count())
                .select_from(TrafficAccountingState)
                .where(TrafficAccountingState.next_refresh_at <= now)
            )
        assert processed == count
        assert failed == pending == 0
        assert calls['patch'] == count
        assert calls['maximum'] == 4
        assert calls['active'] == 0
        print(
            f'LOCAL_FAKE_PANEL_WORKER count={count} duration_seconds={duration:.3f} '
            f'gets={calls["get"]} patches={calls["patch"]} max_parallel={calls["maximum"]}'
        )
