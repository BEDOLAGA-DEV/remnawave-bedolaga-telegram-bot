"""Отмена идущей пробы: ручка API тем же ключом, висящий POST возвращает частичный результат,
статус — отменена; до отправки отменять нечего; «уже завершилась» у API — не ошибка."""

from __future__ import annotations

import pytest

from app.database.crud import reachability as crud
from app.external.bschek_api import BschekAPIError
from app.services.reachability.jobs import (
    PHASE_CANCELLING,
    PHASE_WAITING,
    STATUS_CANCELLED,
    JobNotCancellable,
    RunnerConfig,
)
from app.services.reachability.kinds import KIND_PROBE
from tests.fixtures.bschek_fixtures import load_bschek_fixture
from tests.services.reachability.fakes import FakeAPI, FakeClock
from tests.services.reachability.test_jobs import EU, load, make_job, make_runner


pytestmark = pytest.mark.asyncio


async def _mark_waiting(session_factory, job_id: int) -> None:
    async with session_factory() as db:
        job = await crud.get_job(db, job_id)
        await crud.update_job(db, job, status='running', phase=PHASE_WAITING)
        await db.commit()


async def test_cancel_probe_marks_cancelled_with_partial_cost(session_factory) -> None:
    fx = load_bschek_fixture('p1_probe')
    api = FakeAPI(
        {
            'probe': [BschekAPIError(code='request_in_progress', message='wait', status=409), fx['body']],
            'cancel_probe': [{'run_id': 'r1', 'stopped_jobs': 1, 'charged_credits': 0, 'refunded_credits': 0}],
        }
    )
    cfg = RunnerConfig(probe_retrieve_max=60.0, probe_retrieve_fast_interval=15.0)
    job_id = await make_job(session_factory, KIND_PROBE, fx['request'], [EU], ['mts|пфо|on'])
    runner = make_runner(session_factory, api, FakeClock(), config=cfg)
    await _mark_waiting(session_factory, job_id)

    async with session_factory() as db:
        job = await crud.get_job(db, job_id)
        await runner.cancel(db, job)
        assert job.phase == PHASE_CANCELLING
        key = job.idempotency_key
    assert api.calls[-1] == ('cancel_probe', (key,))

    await runner.resume(job_id)  # висящий POST «вернулся сам» с тем, что успели измерить
    job = await load(session_factory, job_id)
    assert (job.status, job.phase, job.cost_kopeks) == (STATUS_CANCELLED, None, 18)
    assert len(job.legs) == 1


async def test_cancel_probe_when_api_says_already_finished_is_fine(session_factory) -> None:
    api = FakeAPI({'cancel_probe': [BschekAPIError(code='not_found', message='gone', status=404)]})
    job_id = await make_job(session_factory, KIND_PROBE, {'target': 'x'}, [EU], ['mts|пфо|on'])
    runner = make_runner(session_factory, api, FakeClock())
    await _mark_waiting(session_factory, job_id)
    async with session_factory() as db:
        job = await crud.get_job(db, job_id)
        await runner.cancel(db, job)  # результат придёт обычным ответом, падать не надо
        assert job.phase == PHASE_CANCELLING


async def test_cancel_probe_before_submit_is_rejected(session_factory) -> None:
    job_id = await make_job(session_factory, KIND_PROBE, {'target': 'x'}, [EU], ['mts|пфо|on'])
    runner = make_runner(session_factory, FakeAPI(), FakeClock())
    async with session_factory() as db:
        job = await crud.get_job(db, job_id)  # pending, к API ещё не ходили
        with pytest.raises(JobNotCancellable):
            await runner.cancel(db, job)
