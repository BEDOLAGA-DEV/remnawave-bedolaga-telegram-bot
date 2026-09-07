"""Частичный результат пробы из тела 409 request_in_progress: вердикт по компактной ячейке,
срез для кабинета и запись в задачу на каждом безрезультатном повторе."""

from __future__ import annotations

import pytest

from app.external.bschek_api import BschekAPIError
from app.services.reachability.jobs import PHASE_RETRIEVING, RunnerConfig
from app.services.reachability.kinds import KIND_PROBE
from app.services.reachability.legs import partial_probe_progress
from app.services.reachability.verdict import compact_probe_verdict
from tests.services.reachability.fakes import FakeAPI, FakeClock
from tests.services.reachability.test_jobs import EU, load, make_job, make_runner


def test_compact_verdict_prefers_sni_then_tcp_then_icmp() -> None:
    assert compact_probe_verdict({'sni': {'ok': True}, 'tcp': {'ok': False}}) == 'reachable'
    assert compact_probe_verdict({'sni': {'ok': False}, 'tcp': {'ok': True}}) == 'reachable'
    assert compact_probe_verdict({'sni': {'ok': False}, 'tcp': {'ok': False}}) == 'blocked'
    assert compact_probe_verdict({'tcp': {'ok': True}}) == 'reachable'
    assert compact_probe_verdict({'tcp': {'ok': False, 'error': 'timeout'}}) == 'down'
    assert compact_probe_verdict({'icmp': {'ok': True}}) == 'reachable'
    assert compact_probe_verdict({'icmp': {'ok': False}}) == 'down'
    assert compact_probe_verdict({}) == 'unknown'
    assert compact_probe_verdict(None) == 'unknown'


def test_partial_progress_keeps_order_and_marks_only_done_legs() -> None:
    details = {
        'run_id': 'r1',
        'done': 1,
        'total': 3,
        'elapsed_sec': 42.5,
        'retryable': False,
        'legs': [
            {
                'target': 'a.example:443',
                'operator': 'mts',
                'region': 'ЦФО',
                'dpi': 'on',
                'state': 'done',
                'result': {'tcp': {'ok': True, 'latency_ms': 84}},
            },
            {'target': 'a.example:443', 'operator': 'tele2', 'region': 'ПФО', 'dpi': 'on', 'state': 'running'},
            {
                'target': 'b.example:443',
                'operator': 'mts',
                'region': 'ЦФО',
                'dpi': 'on',
                'state': 'pending',
                'ahead': 2,
            },
        ],
    }
    progress = partial_probe_progress(details)
    assert (progress['done'], progress['total'], progress['elapsed_sec']) == (1, 3, 42.5)
    assert [leg['state'] for leg in progress['legs']] == ['done', 'running', 'pending']
    assert progress['legs'][0] == {
        'target': 'a.example:443',
        'operator': 'mts',
        'region': 'ЦФО',
        'dpi': 'on',
        'state': 'done',
        'verdict': 'reachable',
        'latency_ms': 84,
    }
    assert progress['legs'][1]['verdict'] is None and progress['legs'][1]['latency_ms'] is None


def test_partial_progress_tolerates_missing_fields() -> None:
    assert partial_probe_progress({}) == {'done': 0, 'total': 0, 'elapsed_sec': None, 'legs': []}
    weird = partial_probe_progress({'legs': [{'state': 'done', 'result': None}, 'мусор']})
    assert len(weird['legs']) == 1
    assert weird['legs'][0]['verdict'] == 'unknown' and weird['legs'][0]['target'] == ''


IN_PROGRESS = {
    'run_id': 'r1',
    'done': 1,
    'total': 2,
    'elapsed_sec': 30,
    'retryable': False,
    'legs': [
        {
            'target': 'eu-host.example',
            'operator': 'mts',
            'region': 'ПФО',
            'dpi': 'on',
            'state': 'done',
            'result': {'tcp': {'ok': True, 'latency_ms': 51}},
        },
        {'target': 'eu-host.example', 'operator': 'tele2', 'region': 'ПФО', 'dpi': 'on', 'state': 'running'},
    ],
}


@pytest.mark.asyncio
async def test_runner_stores_partial_progress_while_probe_runs(session_factory) -> None:
    """Пока результат не пришёл, в задаче лежит частичный результат — кабинет показывает «проверяем…» по симкам."""
    api = FakeAPI(
        {'probe': [BschekAPIError(code='request_in_progress', message='wait', status=409, details=IN_PROGRESS)]}
    )
    clock = FakeClock()
    cfg = RunnerConfig(probe_retrieve_max=30.0, probe_retrieve_fast_interval=15.0)
    job_id = await make_job(session_factory, KIND_PROBE, {'target': 'x'}, [EU], ['mts|пфо|on', 'tele2|пфо|on'])
    await make_runner(session_factory, api, clock, config=cfg).run(job_id)

    job = await load(session_factory, job_id)
    assert job.phase == PHASE_RETRIEVING
    partial = job.result['partial']
    assert (partial['done'], partial['total']) == (1, 2)
    assert partial['legs'][0]['verdict'] == 'reachable' and partial['legs'][1]['verdict'] is None
    assert job.result['retrieve']['code'] == 'request_in_progress'


@pytest.mark.asyncio
async def test_runner_leaves_no_partial_when_api_gave_none(session_factory) -> None:
    """Голый 409 без легов — след ответа есть, «partial» не выдумывается."""
    api = FakeAPI({'probe': [BschekAPIError(code='request_in_progress', message='wait', status=409)]})
    cfg = RunnerConfig(probe_retrieve_max=30.0, probe_retrieve_fast_interval=15.0)
    job_id = await make_job(session_factory, KIND_PROBE, {'target': 'x'}, [EU], ['mts|пфо|on'])
    await make_runner(session_factory, api, FakeClock(), config=cfg).run(job_id)

    job = await load(session_factory, job_id)
    assert 'partial' not in job.result and job.result['retrieve']['code'] == 'request_in_progress'
