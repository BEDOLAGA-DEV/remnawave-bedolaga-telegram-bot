"""Пачка проверок: CRUD, нарезка целей по 10, оценка времени, статус из задач, драйвер ≤3 параллельно, отмена."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.database.crud import reachability as crud
from app.services.reachability.batches import (
    batch_cost_kopeks,
    batch_done_targets,
    batch_status_from_jobs,
    chunk_targets,
    estimate_batch_minutes,
)
from app.services.reachability.jobs import RunnerConfig
from tests.fixtures.bschek_fixtures import load_bschek_fixture
from tests.services.reachability.fakes import FakeAPI, FakeClock
from tests.services.reachability.test_jobs import EU, make_job, make_runner


# ---------------------------------------------------------------- CRUD


@pytest.mark.asyncio
async def test_batch_crud_roundtrip(session_factory) -> None:
    async with session_factory() as db:
        batch = await crud.create_batch(
            db,
            status='pending',
            started_by_user_id=None,
            scope={'kind': 'problems', 'host_refs': ['h1', 'h2']},
            request={'units': ['mts|цфо|on'], 'dpi': 'on', 'probes': {'tcp': True, 'sni': True}, 'sni_hosts': []},
            total_targets=2,
            estimated_kopeks=1280,
        )
        await db.commit()

        loaded = await crud.get_batch(db, batch.id)
        assert loaded.scope['kind'] == 'problems' and loaded.total_targets == 2 and loaded.jobs == []
        assert (await crud.get_active_batch(db)).id == batch.id
        items, total = await crud.list_batches(db)
        assert total == 1 and items[0].id == batch.id
        assert [item.id for item in await crud.list_unfinished_batches(db)] == [batch.id]

        await crud.update_batch(db, loaded, status='done')
        await db.commit()
        assert await crud.get_active_batch(db) is None
        assert await crud.list_unfinished_batches(db) == []


@pytest.mark.asyncio
async def test_jobs_for_batch_are_ordered_and_carry_legs(session_factory) -> None:
    async with session_factory() as db:
        batch = await crud.create_batch(
            db,
            status='pending',
            started_by_user_id=None,
            scope={'kind': 'manual', 'host_refs': []},
            request={},
            total_targets=2,
            estimated_kopeks=None,
        )
        await db.commit()
        batch_id = batch.id
    ids = [
        await make_job(session_factory, 'probe', {'target': 'x'}, [EU], ['mts|пфо|on'], batch_id=batch_id)
        for _ in range(2)
    ]
    async with session_factory() as db:
        jobs = await crud.jobs_for_batch(db, batch_id)
        assert [job.id for job in jobs] == ids and all(job.legs == [] for job in jobs)
        assert (await crud.get_batch(db, batch_id)).jobs[0].id == ids[0]


# ---------------------------------------------------------------- правила


def test_chunk_targets_by_ten() -> None:
    assert chunk_targets(list(range(23))) == [list(range(10)), list(range(10, 20)), [20, 21, 22]]
    assert chunk_targets([]) == []


def test_estimate_minutes_grows_with_rounds_and_units() -> None:
    # Один раунд: 3 + 0,8·15 = 15 минут; два чанка идут параллельно — тот же раунд; четыре — два раунда.
    assert estimate_batch_minutes(1, 15) == 15
    assert estimate_batch_minutes(2, 15) == 15
    assert estimate_batch_minutes(4, 15) == 30
    assert estimate_batch_minutes(1, 1) >= 1
    assert estimate_batch_minutes(0, 0) >= 1


def _job(status: str, cost: int | None = None, targets: int = 1) -> SimpleNamespace:
    return SimpleNamespace(status=status, cost_kopeks=cost, targets=[{}] * targets)


def test_batch_status_rules() -> None:
    assert batch_status_from_jobs([_job('done'), _job('running')], cancelling=False) is None
    assert batch_status_from_jobs([_job('done'), _job('pending')], cancelling=True) is None
    assert batch_status_from_jobs([_job('done'), _job('done')], cancelling=False) == 'done'
    assert batch_status_from_jobs([_job('done'), _job('failed')], cancelling=False) == 'done'
    assert batch_status_from_jobs([_job('failed'), _job('failed')], cancelling=False) == 'failed'
    assert batch_status_from_jobs([_job('done'), _job('cancelled')], cancelling=False) == 'cancelled'
    assert batch_status_from_jobs([_job('done')], cancelling=True) == 'cancelled'
    assert batch_status_from_jobs([], cancelling=False) == 'done'


def test_batch_cost_and_done_targets() -> None:
    jobs = [_job('done', 640, 10), _job('cancelled', 200, 10), _job('pending', None, 3)]
    assert batch_cost_kopeks(jobs) == 840 and batch_done_targets(jobs) == 20
    assert batch_cost_kopeks([_job('pending')]) is None


# ---------------------------------------------------------------- драйвер


async def make_batch(session_factory, request: dict, targets: list[dict], count: int, **extra) -> tuple[int, list[int]]:
    async with session_factory() as db:
        batch = await crud.create_batch(
            db,
            status=extra.pop('status', 'pending'),
            started_by_user_id=None,
            scope={'kind': 'manual', 'host_refs': []},
            request={'units': ['mts|пфо|on'], 'dpi': 'on', 'probes': {'tcp': True}, 'sni_hosts': []},
            total_targets=count,
            estimated_kopeks=100 * count,
            **extra,
        )
        await db.commit()
        batch_id = batch.id
    job_ids = [
        await make_job(session_factory, 'probe', request, targets, ['mts|пфо|on'], batch_id=batch_id)
        for _ in range(count)
    ]
    return batch_id, job_ids


@pytest.mark.asyncio
async def test_batch_driver_runs_at_most_three_jobs_at_once(session_factory) -> None:
    fx = load_bschek_fixture('p1_probe')
    api = FakeAPI({'probe': [fx['body']]})
    batch_id, _ = await make_batch(session_factory, fx['request'], [EU], 5)
    runner = make_runner(session_factory, api, FakeClock())
    peak = 0
    original_spawn = runner.spawn

    def counting_spawn(job_id: int):
        nonlocal peak
        task = original_spawn(job_id)
        peak = max(peak, sum(1 for item in runner._tasks.values() if not item.done()))
        return task

    runner.spawn = counting_spawn
    await runner.run_batch(batch_id)

    async with session_factory() as db:
        batch = await crud.get_batch(db, batch_id)
        assert (batch.status, batch.phase, batch.cost_kopeks) == ('done', None, 18 * 5)
        assert batch.started_at is not None and batch.finished_at is not None
        assert all(job.status == 'done' for job in batch.jobs)
    assert peak <= 3
    assert len([call for call in api.calls if call[0] == 'probe']) == 5


@pytest.mark.asyncio
async def test_cancel_batch_stops_pending_jobs_and_finishes_cancelled(session_factory) -> None:
    fx = load_bschek_fixture('p1_probe')
    api = FakeAPI({'probe': [fx['body']]})
    batch_id, _ = await make_batch(session_factory, fx['request'], [EU], 4)
    runner = make_runner(session_factory, api, FakeClock())
    async with session_factory() as db:
        batch = await crud.get_batch(db, batch_id)
        await runner.cancel_batch(db, batch)  # до старта: очередь гаснет локально, к API не ходим
    await runner.run_batch(batch_id)

    async with session_factory() as db:
        batch = await crud.get_batch(db, batch_id)
        assert (batch.status, batch.phase) == ('cancelled', None)
        assert {job.status for job in batch.jobs} == {'cancelled'}
    assert api.calls == []


@pytest.mark.asyncio
async def test_sweep_resumes_unfinished_batch(session_factory) -> None:
    fx = load_bschek_fixture('p1_probe')
    api = FakeAPI({'probe': [fx['body']]})
    batch_id, _ = await make_batch(session_factory, fx['request'], [EU], 1, status='running')
    runner = make_runner(session_factory, api, FakeClock(), config=RunnerConfig(sweep_min_age_sec=0))
    await runner.sweep()
    assert runner.is_batch_active(batch_id)
    await asyncio.gather(*list(runner._batch_tasks.values()), *list(runner._tasks.values()))

    async with session_factory() as db:
        assert (await crud.get_batch(db, batch_id)).status == 'done'
