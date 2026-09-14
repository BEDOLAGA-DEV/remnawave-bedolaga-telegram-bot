"""Сервис пачки: превью суммой по чашкам ≤10 целей, запуск создаёт задачу на чашку и драйвер,
пустой выбор и нехватка денег отклоняются, отмена доводит пачку до итога."""

from __future__ import annotations

import asyncio

import pytest

from app.database.crud import reachability as crud
from app.external.remnawave_api import RemnaWaveHost
from app.services.reachability.batches import estimate_batch_minutes
from tests.fixtures.bschek_fixtures import load_bschek_fixture
from tests.services.reachability.test_service import FakeClient, FakePanel, _admin, make_service


class ManyHostsPanel(FakePanel):
    """23 хоста → три чашки: 10 + 10 + 3."""

    async def get_all_hosts(self):
        return [
            RemnaWaveHost(uuid=f'h{i}', remark=f'Host {i}', address=f'h{i}.example', port=443, sni=None)
            for i in range(1, 24)
        ]


def payload(count: int = 23, **extra) -> dict:
    return {
        'host_refs': [f'h{i}' for i in range(1, count + 1)],
        'units': ['mts'],
        'dpi': 'on',
        'probes': {'tcp': True, 'sni': True},
        'sni_hosts': [],
        'scope_kind': 'all',
        **extra,
    }


@pytest.mark.asyncio
async def test_preview_batch_sums_chunks_and_estimates_time(session_factory) -> None:
    service = make_service(session_factory, panel=ManyHostsPanel())
    async with session_factory() as db:
        preview = await service.preview_batch(db, payload())
    assert len(preview.chunks) == 3 and len(preview.targets) == 23
    assert [len(chunk.targets) for chunk in preview.chunks] == [10, 10, 3]
    assert preview.cost_kopeks == 18 * 3  # pv_bare_mts: 18 за чашку
    assert preview.units_resolved == ['mts|пфо|on']
    assert preview.estimated_minutes == estimate_batch_minutes(3, 1)
    assert preview.balance_kopeks is not None


@pytest.mark.asyncio
async def test_preview_batch_rejects_empty_and_oversized_scope(session_factory) -> None:
    service = make_service(session_factory, panel=ManyHostsPanel())
    async with session_factory() as db:
        with pytest.raises(ValueError, match='хотя бы один'):
            await service.preview_batch(db, payload(0))
        with pytest.raises(ValueError, match='не больше'):
            await service.preview_batch(db, {**payload(), 'host_refs': [f'h{i}' for i in range(1, 302)]})


@pytest.mark.asyncio
async def test_create_batch_makes_one_job_per_chunk_and_spawns_driver(session_factory) -> None:
    client = FakeClient({'probe': [load_bschek_fixture('p1_probe')['body']]})
    service = make_service(session_factory, client=client, panel=ManyHostsPanel())
    async with session_factory() as db:
        admin = await _admin(db)
        await db.commit()
        batch = await service.create_batch(db, payload(scope_kind='problems'), admin.id)
        jobs = await crud.jobs_for_batch(db, batch.id)
        assert [len(job.targets) for job in jobs] == [10, 10, 3]
        assert len({job.idempotency_key for job in jobs}) == 3
        assert all(job.batch_id == batch.id and job.status == 'pending' for job in jobs)
        assert (batch.total_targets, batch.scope['kind'], batch.estimated_kopeks) == (23, 'problems', 54)
        assert batch.scope['host_refs'][:2] == ['h1', 'h2']
        assert service.runner.is_batch_active(batch.id)
    await asyncio.gather(*service.runner._batch_tasks.values(), *service.runner._tasks.values())
    async with session_factory() as db:
        done = await service.get_batch(db, batch.id)
        assert done.status == 'done' and done.cost_kopeks == 18 * 3


@pytest.mark.asyncio
async def test_create_batch_refuses_when_balance_is_short(session_factory) -> None:
    service = make_service(session_factory, panel=ManyHostsPanel())

    async def poor_balance() -> int | None:
        return 10

    service._balance_kopeks = poor_balance
    async with session_factory() as db:
        admin = await _admin(db)
        await db.commit()
        with pytest.raises(ValueError, match='не хватает'):
            await service.create_batch(db, payload(), admin.id)
        assert (await crud.list_batches(db))[1] == 0


@pytest.mark.asyncio
async def test_cancel_batch_before_start_finishes_it_cancelled(session_factory) -> None:
    service = make_service(session_factory, panel=ManyHostsPanel())
    async with session_factory() as db:
        admin = await _admin(db)
        await db.commit()
        batch = await crud.create_batch(
            db,
            status='pending',
            started_by_user_id=admin.id,
            scope={'kind': 'all', 'host_refs': []},
            request={},
            total_targets=0,
            estimated_kopeks=None,
        )
        await db.commit()
        cancelled = await service.cancel_batch(db, batch.id)
        assert cancelled.phase == 'cancelling'
    await asyncio.gather(*service.runner._batch_tasks.values())
    async with session_factory() as db:
        assert (await service.get_batch(db, batch.id)).status == 'cancelled'
