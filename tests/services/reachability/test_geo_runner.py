"""Жизненный цикл задачи GEO на фейковом API: запуск → опрос → done/empty/error; отмена; 404; too_many_active."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.database.crud import reachability as crud
from app.database.models import User
from app.external.bschek_api import BschekAPIError
from app.services.reachability.gate import PaidCallGate
from app.services.reachability.geo_result import recheck_started
from app.services.reachability.jobs import (
    GEO_CANNOT_CANCEL_NOTE,
    GEO_EMPTY_NOTE,
    PHASE_POLLING,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    JobRunner,
    RunnerConfig,
)
from app.services.reachability.kinds import KIND_GEO
from tests.services.reachability.fakes import FakeAPI, FakeClock


pytestmark = pytest.mark.asyncio

REQUEST = {'targets': ['example.com:443'], 'network': 'res', 'probe_mode': 'tls', 'heavy': False, 'core': ''}
TARGETS = [
    {
        'kind': 'custom',
        'label': 'example.com',
        'address': 'example.com',
        'port': 443,
        'target_key': 'example.com:443',
        'sni': None,
        'ref': {},
        'purpose': 'unknown',
        'raw_link': None,
    }
]
START = {
    'outcome': 'queued',
    'run_id': 812,
    'state': 'running',
    'poll': '/v1/geo/runs/812',
    'n_nodes': 2,
    'reserve_credits': 90,
    'estimated_sec': 10,
}
ROW_OK = {
    'region': 'moscow',
    'city': 'moscow',
    'provider': 'mts',
    'verdict': 'ok',
    'targets': {'example.com:443': {'ok': True, 'ms': 120, 'kind': 'tls'}},
    'mb_bill': 0.1,
}
ROW_BLOCKED = {
    'region': 'voronezh_oblast',
    'city': 'voronezh',
    'provider': 'rt',
    'verdict': 'blocked',
    'targets': {'example.com:443': {'ok': False, 'ms': None, 'kind': 'tls', 'err': 'timeout'}},
    'mb_bill': 0.1,
}
RUNNING = {'state': 'running', 'progress': {'done': 1, 'total': 2}, 'rows': [ROW_OK]}
DONE = {
    'state': 'done',
    'result_ready': True,
    'progress': {'done': 2, 'total': 2},
    'rows': [ROW_OK, ROW_BLOCKED],
    'by_verdict': {'ok': 1, 'blocked': 1},
    'conclusion': {'code': 'mixed', 'text': 'Смешанная картина'},
    'charged_credits': 20,
    'mb_bill': 0.2,
}
EMPTY = {
    'state': 'empty',
    'result_ready': True,
    'progress': {'done': 0, 'total': 2},
    'rows': [],
    'by_verdict': {},
    'charged_credits': 0,
}
ERROR = {'state': 'error', 'result_ready': True, 'error': 'engine exploded', 'rows': []}
ABORTED = {
    'state': 'aborted',
    'result_ready': True,
    'progress': {'done': 1, 'total': 2},
    'rows': [ROW_OK],
    'by_verdict': {'ok': 1},
    'charged_credits': 10,
}


async def make_geo_job(session_factory, **extra) -> int:
    async with session_factory() as db:
        admin = (await db.execute(select(User).where(User.telegram_id == 1))).scalar_one_or_none()
        if admin is None:
            admin = User(telegram_id=1, username='admin', first_name='A', language='ru')
            db.add(admin)
            await db.flush()
        fields = {
            'kind': KIND_GEO,
            'status': 'pending',
            'trigger': 'manual',
            'started_by_user_id': admin.id,
            'idempotency_key': f'geo-{datetime.now(UTC).timestamp()}',
            'request': REQUEST,
            'targets': TARGETS,
            'units_requested': [],
            'units_resolved': ['geo'],
            'dpi': 'any',
            'estimated_kopeks': 90,
            'estimate_is_exact': False,
        }
        fields.update(extra)
        job = await crud.create_job(db, **fields)
        await db.commit()
        return job.id


def make_runner(session_factory, api: FakeAPI, clock: FakeClock, **cfg) -> JobRunner:
    return JobRunner(
        client_factory=lambda: api,
        gate=PaidCallGate(min_interval=0, clock=clock, sleep=clock.sleep),
        session_factory=session_factory,
        cost_limit_kopeks=lambda: 0,
        config=RunnerConfig(**cfg),
        sleep=clock.sleep,
        clock=clock,
    )


async def load(session_factory, job_id: int):
    async with session_factory() as db:
        return await crud.get_job(db, job_id)


async def test_geo_job_runs_to_done_with_rows_summary_and_charged_cost(session_factory) -> None:
    api = FakeAPI({'geo_start': [START], 'geo_run': [RUNNING, DONE]})
    clock = FakeClock()
    job_id = await make_geo_job(session_factory)
    await make_runner(session_factory, api, clock).run(job_id)
    job = await load(session_factory, job_id)
    assert job.status == STATUS_DONE and job.external_id == 812 and job.phase is None
    assert job.cost_kopeks == 20 and job.refunded_kopeks == 70
    assert job.result['status']['state'] == 'done'
    assert [row['verdict'] for row in job.result['rows']] == ['ok', 'blocked']
    assert job.result['rows'][0]['is_result'] is True and job.result['rows'][0]['latency_ms'] == 120
    assert job.result['summary']['by_verdict'] == {'ok': 1, 'blocked': 1}
    assert job.result['summary']['conclusion']['code'] == 'mixed'
    assert job.result['geo']['n_nodes'] == 2 and job.result['geo']['scope_label'] == 'сайты · проводной · вся РФ'
    assert job.units_effective == ['geo'] and job.estimated_kopeks == 90
    assert clock.sleeps.count(5.0) >= 2, 'опрос раз в пять секунд'
    assert [call[0] for call in api.calls] == ['geo_start', 'geo_run', 'geo_run']


async def test_progress_and_rows_are_visible_while_running(session_factory) -> None:
    api = FakeAPI({'geo_start': [START], 'geo_run': [RUNNING, RUNNING, DONE]})
    clock = FakeClock()
    job_id = await make_geo_job(session_factory)
    runner = make_runner(session_factory, api, clock)
    seen: list[dict] = []
    original = runner._update

    async def spy(db, job, **fields):
        if 'result' in fields and fields['result'].get('status', {}).get('state') == 'running':
            seen.append(fields['result'])
        await original(db, job, **fields)

    runner._update = spy
    await runner.run(job_id)
    assert seen, 'пока прогон идёт, строки и прогресс пишутся в задачу'
    assert seen[0]['summary']['progress'] == {'done': 1, 'total': 2}
    assert [row['city'] for row in seen[0]['rows']] == ['moscow']
    job = await load(session_factory, job_id)
    assert job.status == STATUS_DONE and job.result['summary']['progress'] == {'done': 2, 'total': 2}


async def test_empty_run_is_done_with_zero_cost_and_a_note(session_factory) -> None:
    api = FakeAPI({'geo_start': [START], 'geo_run': [EMPTY]})
    job_id = await make_geo_job(session_factory)
    await make_runner(session_factory, api, FakeClock()).run(job_id)
    job = await load(session_factory, job_id)
    assert job.status == STATUS_DONE and job.cost_kopeks == 0 and job.refunded_kopeks == 90
    assert job.result['note'] == GEO_EMPTY_NOTE == 'Трафика не было, резерв возвращён'


async def test_error_state_fails_the_job_with_service_message(session_factory) -> None:
    api = FakeAPI({'geo_start': [START], 'geo_run': [ERROR]})
    job_id = await make_geo_job(session_factory)
    await make_runner(session_factory, api, FakeClock()).run(job_id)
    job = await load(session_factory, job_id)
    assert job.status == STATUS_FAILED and job.error_code == 'geo_error'
    assert 'engine exploded' in job.error_message
    assert job.result['status']['state'] == 'error'


async def test_cancel_stops_remote_and_keeps_checked_rows(session_factory) -> None:
    api = FakeAPI({'geo_start': [START], 'geo_run': [RUNNING, ABORTED], 'geo_cancel': [{'ok': True}]})
    clock = FakeClock()
    job_id = await make_geo_job(session_factory)
    runner = make_runner(session_factory, api, clock)
    async with session_factory() as db:
        job = await crud.get_job(db, job_id)
        await runner._update(db, job, external_id=812, status=STATUS_RUNNING, phase=PHASE_POLLING)
        await runner.cancel(db, job)
    assert ('geo_cancel', (812,)) in api.calls
    await runner.resume(job_id)
    job = await load(session_factory, job_id)
    assert job.status == STATUS_CANCELLED and job.cost_kopeks == 10 and job.refunded_kopeks == 80
    assert len(job.result['rows']) == 1


async def test_not_running_on_cancel_is_not_an_error(session_factory) -> None:
    api = FakeAPI({'geo_cancel': [BschekAPIError(code='not_running', message='already done', status=409)]})
    job_id = await make_geo_job(session_factory, external_id=812, status=STATUS_RUNNING, phase=PHASE_POLLING)
    runner = make_runner(session_factory, api, FakeClock())
    async with session_factory() as db:
        job = await crud.get_job(db, job_id)
        await runner.cancel(db, job)  # не бросает


async def test_cannot_cancel_marks_the_job_and_leaves_it_to_the_poller(session_factory) -> None:
    api = FakeAPI({'geo_cancel': [BschekAPIError(code='cannot_cancel', message='orphaned', status=409)]})
    job_id = await make_geo_job(session_factory, external_id=812, status=STATUS_RUNNING, phase=PHASE_POLLING)
    runner = make_runner(session_factory, api, FakeClock())
    async with session_factory() as db:
        job = await crud.get_job(db, job_id)
        await runner.cancel(db, job)
    job = await load(session_factory, job_id)
    assert job.status == STATUS_RUNNING and job.result['note'] == GEO_CANNOT_CANCEL_NOTE
    assert 'закроет' in GEO_CANNOT_CANCEL_NOTE


async def test_too_many_active_on_start_is_retried_then_succeeds(session_factory) -> None:
    busy = BschekAPIError(code='too_many_active', message='3 runs', status=409, retryable=True, retry_after=15.0)
    api = FakeAPI({'geo_start': [busy, START], 'geo_run': [DONE]})
    clock = FakeClock()
    job_id = await make_geo_job(session_factory)
    await make_runner(session_factory, api, clock).run(job_id)
    job = await load(session_factory, job_id)
    assert job.status == STATUS_DONE and 15.0 in clock.sleeps


async def test_rate_limited_on_start_waits_retry_after(session_factory) -> None:
    limited = BschekAPIError(code='rate_limited', message='slow down', status=429, retry_after=3.0)
    api = FakeAPI({'geo_start': [limited, START], 'geo_run': [DONE]})
    clock = FakeClock()
    job_id = await make_geo_job(session_factory)
    await make_runner(session_factory, api, clock).run(job_id)
    job = await load(session_factory, job_id)
    assert job.status == STATUS_DONE and 3.0 in clock.sleeps


async def test_run_vanished_on_poll_fails_in_words(session_factory) -> None:
    api = FakeAPI({'geo_start': [START], 'geo_run': [BschekAPIError(code='not_found', message='nf', status=404)]})
    job_id = await make_geo_job(session_factory)
    await make_runner(session_factory, api, FakeClock()).run(job_id)
    job = await load(session_factory, job_id)
    assert job.status == STATUS_FAILED and job.error_code == 'not_found'
    assert job.error_message == 'Прогон пропал на стороне сервиса'


async def test_start_without_run_id_fails_without_blind_retry(session_factory) -> None:
    api = FakeAPI({'geo_start': [{'outcome': 'weird'}]})
    job_id = await make_geo_job(session_factory)
    await make_runner(session_factory, api, FakeClock()).run(job_id)
    job = await load(session_factory, job_id)
    assert job.status == STATUS_FAILED and job.error_code == 'unexpected_response'
    assert len([call for call in api.calls if call[0] == 'geo_start']) == 1


async def test_poll_timeout_grows_with_the_estimate_and_is_capped(session_factory) -> None:
    api = FakeAPI({'geo_start': [{**START, 'estimated_sec': 10_000}], 'geo_run': [RUNNING]})
    clock = FakeClock()
    job_id = await make_geo_job(session_factory)
    await make_runner(session_factory, api, clock).run(job_id)
    job = await load(session_factory, job_id)
    assert job.status == STATUS_RUNNING, 'таймаут опроса — не провал: доберёт обходчик'
    polls = len([call for call in api.calls if call[0] == 'geo_run'])
    assert polls == 900 // 5, 'потолок 900 с при опросе раз в 5 с'


# ---------------------------------------------------------------- повтор города — в тот же тест

RECHECK_KEY = 'moscow|moscow|'
RECHECK_REQUEST = {**REQUEST, 'cities': [{'region': 'moscow', 'city': 'moscow'}]}
VORONEZH_KEY = 'voronezh_oblast|voronezh|'


def _parent_rows() -> list[dict]:
    return [
        {
            'region': 'moscow',
            'city': 'moscow',
            'req_isp': None,
            'provider': 'old',
            'exit_ip': '1.1.1.1',
            'verdict': 'blocked',
            'is_result': True,
        },
        {
            'region': 'voronezh_oblast',
            'city': 'voronezh',
            'req_isp': None,
            'provider': 'rt',
            'exit_ip': '2.2.2.2',
            'verdict': 'ok',
            'is_result': True,
        },
    ]


def _parent_result(*keys: str) -> dict:
    result = {
        'rows': _parent_rows(),
        'summary': {'by_verdict': {'blocked': 1, 'ok': 1}, 'conclusion': {'text': 'старое'}},
        'geo': {'n_nodes': 2},
    }
    for key in keys:
        result = recheck_started(
            result, key, same_exit=False, reserve_kopeks=90, started_at='2026-09-11T10:00:00+00:00', admin_id=None
        )
    return result


async def _done_parent(session_factory, *keys: str) -> int:
    return await make_geo_job(
        session_factory,
        status=STATUS_DONE,
        estimated_kopeks=1384,
        cost_kopeks=1013,
        refunded_kopeks=371,
        result=_parent_result(*(keys or (RECHECK_KEY,))),
    )


async def _count_jobs(session_factory) -> int:
    async with session_factory() as db:
        return (await crud.list_jobs(db))[1]


async def test_recheck_writes_rows_and_money_into_the_parent_and_makes_no_job(session_factory) -> None:
    parent_id = await _done_parent(session_factory)
    fresh = {**ROW_OK, 'exit_ip': '9.9.9.9'}
    done = {**DONE, 'rows': [fresh], 'by_verdict': {'ok': 1}, 'charged_credits': 3}
    api = FakeAPI({'geo_start': [{**START, 'n_nodes': 1, 'reserve_credits': 95}], 'geo_run': [RUNNING, done]})
    runner = make_runner(session_factory, api, FakeClock())
    await runner.rechecks.run(parent_id, RECHECK_KEY, request=RECHECK_REQUEST, reserve_kopeks=90)
    parent = await load(session_factory, parent_id)
    rows = parent.result['rows']
    assert [row['city'] for row in rows] == ['moscow', 'voronezh', 'moscow']
    assert rows[0]['rechecked'] is True and rows[2]['new_exit'] is True and rows[2]['recheck_run_id'] == 812
    assert rows[2]['verdict'] == 'ok' and rows[2]['exit_ip'] == '9.9.9.9'
    assert parent.result['summary']['by_verdict'] == {'blocked': 1, 'ok': 2}
    assert parent.result['summary']['conclusion'] is None and parent.result['geo'] == {'n_nodes': 2}
    assert parent.result['rechecks'] == {}, 'запись идущего повтора снята'
    assert parent.status == STATUS_DONE
    # Деньги — в родителя: резерв из ответа на запуск (95), списано 3, разница возвращена.
    assert (parent.estimated_kopeks, parent.cost_kopeks, parent.refunded_kopeks) == (1384 + 95, 1013 + 3, 371 + 92)
    assert await _count_jobs(session_factory) == 1, 'история не растёт'
    start_call = next(call for call in api.calls if call[0] == 'geo_start')
    assert start_call[1][0], 'запуск — со своим ключом идемпотентности'
    assert not runner.rechecks.is_active(parent_id, RECHECK_KEY)


async def test_recheck_failure_lands_in_the_entry_in_words_and_leaves_the_report_alone(session_factory) -> None:
    parent_id = await _done_parent(session_factory)
    api = FakeAPI({'geo_start': [START], 'geo_run': [ERROR]})
    await make_runner(session_factory, api, FakeClock()).rechecks.run(
        parent_id, RECHECK_KEY, request=RECHECK_REQUEST, reserve_kopeks=90
    )
    parent = await load(session_factory, parent_id)
    entry = parent.result['rechecks'][RECHECK_KEY]
    assert entry['status'] == 'failed' and entry['error'] == 'engine exploded' and entry['run_id'] == 812
    assert [row['city'] for row in parent.result['rows']] == ['moscow', 'voronezh']
    assert 'rechecked' not in parent.result['rows'][0]
    assert (parent.estimated_kopeks, parent.cost_kopeks, parent.refunded_kopeks) == (1384, 1013, 371)


async def test_recheck_vanished_run_and_timeout_fail_the_entry_in_words(session_factory) -> None:
    parent_id = await _done_parent(session_factory)
    vanished = BschekAPIError(code='not_found', message='nf', status=404)
    api = FakeAPI({'geo_start': [START], 'geo_run': [vanished]})
    await make_runner(session_factory, api, FakeClock()).rechecks.run(
        parent_id, RECHECK_KEY, request=RECHECK_REQUEST, reserve_kopeks=90
    )
    parent = await load(session_factory, parent_id)
    assert parent.result['rechecks'][RECHECK_KEY]['error'] == 'Прогон пропал на стороне сервиса'
    # Сервис так и не отдал итог: запись падает словами, а не висит «идёт проверка» навсегда.
    parent_id = await _done_parent(session_factory)
    api = FakeAPI({'geo_start': [{**START, 'estimated_sec': 10_000}], 'geo_run': [RUNNING]})
    await make_runner(session_factory, api, FakeClock()).rechecks.run(
        parent_id, RECHECK_KEY, request=RECHECK_REQUEST, reserve_kopeks=90
    )
    parent = await load(session_factory, parent_id)
    entry = parent.result['rechecks'][RECHECK_KEY]
    assert entry['status'] == 'failed' and '15 мин' in entry['error'] and '812' in entry['error']
    assert len([call for call in api.calls if call[0] == 'geo_run']) == 900 // 5, 'потолок 900 с раз в 5 с'


class _TwoRunsAPI(FakeAPI):
    """Два повтора на одном родителе: у каждого свой прогон, свой ответ."""

    async def geo_start(self, body: dict, key: str) -> dict:
        self.calls.append(('geo_start', (key,)))
        return {**START, 'run_id': 812 if body['cities'][0]['city'] == 'moscow' else 813}

    async def geo_run(self, run_id: int) -> dict:
        self.calls.append(('geo_run', (run_id,)))
        row = (
            {**ROW_OK, 'exit_ip': '9.9.9.9'}
            if run_id == 812
            else {**ROW_BLOCKED, 'verdict': 'ok', 'exit_ip': '8.8.8.8'}
        )
        return {**DONE, 'rows': [row], 'charged_credits': 3}


async def test_two_rechecks_on_one_parent_do_not_lose_each_other(session_factory) -> None:
    parent_id = await _done_parent(session_factory, RECHECK_KEY, VORONEZH_KEY)
    runner = make_runner(session_factory, _TwoRunsAPI(), FakeClock())
    voronezh = {**REQUEST, 'cities': [{'region': 'voronezh_oblast', 'city': 'voronezh'}]}
    await asyncio.gather(
        runner.rechecks.run(parent_id, RECHECK_KEY, request=RECHECK_REQUEST, reserve_kopeks=90),
        runner.rechecks.run(parent_id, VORONEZH_KEY, request=voronezh, reserve_kopeks=90),
    )
    parent = await load(session_factory, parent_id)
    exits = sorted(row['exit_ip'] for row in parent.result['rows'] if row.get('recheck_run_id'))
    assert exits == ['8.8.8.8', '9.9.9.9'], 'оба итога на месте — записи не затирают друг друга'
    assert parent.result['rechecks'] == {}
    assert parent.cost_kopeks == 1013 + 6 and parent.estimated_kopeks == 1384 + 180
