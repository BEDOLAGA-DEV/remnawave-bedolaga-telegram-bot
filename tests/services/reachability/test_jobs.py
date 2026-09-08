"""Жизненный цикл задач на фейковом API: 524 → повтор ключом → 200; VLESS/скан с опросом;
потолок цены; занятость; отмена; таймаут опроса и обходчик."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database.crud import reachability as crud
from app.database.models import User
from app.external.bschek_api import BschekAPIError, BschekGatewayError
from app.services.reachability.gate import PaidCallGate
from app.services.reachability.jobs import (
    PHASE_CANCELLING,
    PHASE_RETRIEVING,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    JobNotCancellable,
    JobRunner,
    RunnerConfig,
)
from app.services.reachability.kinds import KIND_PROBE, KIND_SCAN, KIND_VLESS
from app.services.reachability.targets import Target
from tests.fixtures.bschek_fixtures import load_bschek_fixture
from tests.services.reachability.fakes import FakeAPI, FakeClock


pytestmark = pytest.mark.asyncio


def body(name: str) -> dict:
    return load_bschek_fixture(name)['body']


BS = Target(
    kind='host',
    label='BS',
    address='bs-host.example',
    port=9443,
    target_key='bs-host.example:9443',
    sni='whitelisted.example',
    ref={'host_uuid': 'h-bs'},
    purpose='bs',
).as_dict()
EU = Target(
    kind='host',
    label='EU',
    address='eu-host.example',
    port=None,
    target_key='eu-host.example',
    sni='eu-host.example',
    ref={'host_uuid': 'h-eu'},
    purpose='regular',
).as_dict()
CIDR = [{'kind': 'cidr', 'target_key': '192.0.2.0/24'}]


async def make_job(session_factory, kind: str, request: dict, targets: list[dict], units: list[str], **extra) -> int:
    async with session_factory() as db:
        admin = (await db.execute(select(User).where(User.telegram_id == 1))).scalar_one_or_none()
        if admin is None:
            admin = User(telegram_id=1, username='admin', first_name='A', language='ru')
            db.add(admin)
            await db.flush()
        fields = {
            'kind': kind,
            'status': 'pending',
            'trigger': 'manual',
            'started_by_user_id': admin.id,
            'idempotency_key': f'key-{kind}-{datetime.now(UTC).timestamp()}',
            'request': request,
            'targets': targets,
            'units_requested': units,
            'units_resolved': units,
            'dpi': request.get('dpi', 'on'),
            'estimated_kopeks': 100,
            'estimate_is_exact': True,
            **extra,
        }
        job = await crud.create_job(db, **fields)
        await db.commit()
        return job.id


def make_runner(
    session_factory, api: FakeAPI, clock: FakeClock, *, cost_limit: int = 0, config: RunnerConfig | None = None
) -> JobRunner:
    gate = PaidCallGate(min_interval=0, clock=clock, sleep=clock.sleep)
    return JobRunner(
        client_factory=lambda: api,
        gate=gate,
        session_factory=session_factory,
        cost_limit_kopeks=lambda: cost_limit,
        config=config,
        sleep=clock.sleep,
        clock=clock,
    )


async def load(session_factory, job_id: int):
    async with session_factory() as db:
        return await crud.get_job(db, job_id)


# ---------------------------------------------------------------- probe


async def test_probe_happy_path_stores_result_cost_and_legs(session_factory) -> None:
    fx = load_bschek_fixture('p1_probe')
    api = FakeAPI({'probe': [fx['body']]})
    job_id = await make_job(session_factory, KIND_PROBE, fx['request'], [EU], ['mts|пфо|on'])
    await make_runner(session_factory, api, FakeClock()).run(job_id)

    job = await load(session_factory, job_id)
    assert (job.status, job.phase, job.cost_kopeks, job.refunded_kopeks) == (STATUS_DONE, None, 18, 0)
    assert job.units_effective == ['mts|пфо|on']
    assert [leg.verdict for leg in job.legs] == ['down']
    assert job.result['response']['outcome'] == 'done'
    assert job.attempts == 1
    assert job.started_at is not None and job.finished_at is not None


async def test_probe_gateway_timeout_then_in_progress_then_result(session_factory) -> None:
    fx = load_bschek_fixture('p2_replay')
    api = FakeAPI(
        {
            'probe': [
                BschekGatewayError(code='http_524', message='cf', status=524, retryable=True),
                BschekAPIError(code='request_in_progress', message='wait', status=409),
                fx['body'],
            ]
        }
    )
    clock = FakeClock()
    job_id = await make_job(session_factory, KIND_PROBE, fx['request'], [EU, BS], ['*|цфо|on'])
    await make_runner(session_factory, api, clock).run(job_id)

    job = await load(session_factory, job_id)
    assert job.status == STATUS_DONE and job.cost_kopeks == 260 and len(job.legs) == 10
    keys = [call[1][0] for call in api.calls if call[0] == 'probe']
    assert len(keys) == 3 and len(set(keys)) == 1  # все повторы — тем же ключом
    assert clock.sleeps[:2] == [15.0, 15.0]
    assert job.attempts == 3


async def test_probe_left_retrieving_when_result_never_comes(session_factory) -> None:
    api = FakeAPI({'probe': [BschekAPIError(code='request_in_progress', message='wait', status=409)]})
    clock = FakeClock()
    cfg = RunnerConfig(probe_retrieve_max=60.0, probe_retrieve_fast_interval=15.0)
    job_id = await make_job(session_factory, KIND_PROBE, {'target': 'x'}, [EU], ['mts|пфо|on'])
    await make_runner(session_factory, api, clock, config=cfg).run(job_id)

    job = await load(session_factory, job_id)
    assert (job.status, job.phase) == (STATUS_RUNNING, PHASE_RETRIEVING)
    assert clock.now == 60.0


async def test_probe_retrieve_keeps_last_api_answer_on_job(session_factory) -> None:
    """Пока результат не пришёл, в задаче виден последний ответ API — иначе «идёт» ничего не объясняет."""
    api = FakeAPI(
        {
            'probe': [
                BschekGatewayError(code='http_524', message='cf', status=524, retryable=True),
                BschekAPIError(code='request_in_progress', message='wait', status=409, request_id='r9'),
            ]
        }
    )
    cfg = RunnerConfig(probe_retrieve_max=30.0, probe_retrieve_fast_interval=15.0)
    job_id = await make_job(session_factory, KIND_PROBE, {'target': 'x'}, [EU], ['mts|пфо|on'])
    await make_runner(session_factory, api, FakeClock(), config=cfg).run(job_id)

    job = await load(session_factory, job_id)
    assert (job.status, job.phase) == (STATUS_RUNNING, PHASE_RETRIEVING)
    trace = job.result['retrieve']
    assert (trace['code'], trace['status'], trace['request_id']) == ('request_in_progress', 409, 'r9')
    assert trace['attempt'] == job.attempts and trace['at']


async def test_probe_older_than_cap_fails_instead_of_retrying_forever(session_factory) -> None:
    """Обходчик не должен поднимать пробу вечно: старше потолка — падение с внятной причиной, без вызовов API."""
    api = FakeAPI({'probe': [BschekAPIError(code='request_in_progress', message='wait', status=409)]})
    cfg = RunnerConfig(probe_max_age_sec=2700.0)
    job_id = await make_job(
        session_factory,
        KIND_PROBE,
        {'target': 'x'},
        [EU],
        ['mts|пфо|on'],
        status=STATUS_RUNNING,
        phase=PHASE_RETRIEVING,
        started_at=datetime.now(UTC) - timedelta(hours=1),
        attempts=15,
        result={
            'retrieve': {'code': 'request_in_progress', 'status': 409, 'attempt': 15, 'at': 'x', 'request_id': 'r9'}
        },
    )
    await make_runner(session_factory, api, FakeClock(), config=cfg).resume(job_id)

    job = await load(session_factory, job_id)
    assert (job.status, job.error_code, job.retryable) == (STATUS_FAILED, 'probe_stalled', False)
    assert '45 минут' in job.error_message and 'r9' in job.error_message
    assert 'request_in_progress' not in job.error_message  # людям — словами, код не нужен
    assert api.calls == []


async def test_probe_younger_than_cap_keeps_retrieving_on_resume(session_factory) -> None:
    api = FakeAPI({'probe': [BschekAPIError(code='request_in_progress', message='wait', status=409)]})
    cfg = RunnerConfig(probe_max_age_sec=2700.0, probe_retrieve_max=30.0, probe_retrieve_fast_interval=15.0)
    job_id = await make_job(
        session_factory,
        KIND_PROBE,
        {'target': 'x'},
        [EU],
        ['mts|пфо|on'],
        status=STATUS_RUNNING,
        phase=PHASE_RETRIEVING,
        started_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    await make_runner(session_factory, api, FakeClock(), config=cfg).resume(job_id)

    job = await load(session_factory, job_id)
    assert (job.status, job.phase) == (STATUS_RUNNING, PHASE_RETRIEVING)
    assert len(api.calls) == 2


async def test_probe_retrieve_slows_down_after_fast_window(session_factory) -> None:
    api = FakeAPI({'probe': [BschekAPIError(code='request_in_progress', message='wait', status=409)]})
    clock = FakeClock()
    cfg = RunnerConfig(
        probe_retrieve_max=100.0,
        probe_retrieve_fast_interval=15.0,
        probe_retrieve_fast_window=30.0,
        probe_retrieve_slow_interval=30.0,
    )
    job_id = await make_job(session_factory, KIND_PROBE, {'target': 'x'}, [EU], ['mts|пфо|on'])
    await make_runner(session_factory, api, clock, config=cfg).run(job_id)
    assert clock.sleeps == [15.0, 15.0, 30.0, 30.0, 30.0]


async def test_probe_no_dpi_on_race_fails_without_charge(session_factory) -> None:
    api = FakeAPI({'probe': [{'outcome': 'no_dpi_on', 'skipped_dpi_off': [{'operator': 'yota', 'name': 'Yota'}]}]})
    job_id = await make_job(session_factory, KIND_PROBE, {'target': 'x'}, [EU], ['yota|уфо|off'])
    await make_runner(session_factory, api, FakeClock()).run(job_id)
    job = await load(session_factory, job_id)
    assert (job.status, job.error_code, job.cost_kopeks, job.retryable) == (STATUS_FAILED, 'no_dpi_on', None, False)
    assert job.skipped['dpi_off'] == [{'operator': 'yota', 'name': 'Yota'}]


async def test_probe_validation_error_fails_with_api_message(session_factory) -> None:
    api = FakeAPI(
        {'probe': [BschekAPIError(code='no_probes', message='Не выбрано ни одной пробы', status=400, request_id='r1')]}
    )
    job_id = await make_job(session_factory, KIND_PROBE, {'target': 'x'}, [EU], ['mts|пфо|on'])
    await make_runner(session_factory, api, FakeClock()).run(job_id)
    job = await load(session_factory, job_id)
    assert (job.status, job.error_code, job.error_message) == (STATUS_FAILED, 'no_probes', 'Не выбрано ни одной пробы')
    assert (job.last_request_id, job.phase, job.retryable) == ('r1', None, False)


async def test_probe_transient_503_is_retried_with_same_key(session_factory) -> None:
    fx = load_bschek_fixture('p1_probe')
    api = FakeAPI(
        {
            'probe': [
                BschekAPIError(code='worker_unavailable', message='later', status=503, retryable=True, retry_after=5.0),
                fx['body'],
            ]
        }
    )
    clock = FakeClock()
    job_id = await make_job(session_factory, KIND_PROBE, fx['request'], [EU], ['mts|пфо|on'])
    await make_runner(session_factory, api, clock).run(job_id)
    assert (await load(session_factory, job_id)).status == STATUS_DONE
    assert 5.0 in clock.sleeps
    assert len({call[1][0] for call in api.calls}) == 1


async def test_probe_transient_errors_give_up_after_retries(session_factory) -> None:
    err = BschekAPIError(code='worker_unavailable', message='later', status=503, retryable=True)
    api = FakeAPI({'probe': [err]})
    cfg = RunnerConfig(transient_retries=2, transient_default_wait=1.0)
    job_id = await make_job(session_factory, KIND_PROBE, {'target': 'x'}, [EU], ['mts|пфо|on'])
    await make_runner(session_factory, api, FakeClock(), config=cfg).run(job_id)
    job = await load(session_factory, job_id)
    assert (job.status, job.error_code, job.retryable, job.attempts) == (STATUS_FAILED, 'worker_unavailable', True, 3)


async def test_unexpected_exception_marks_internal_error(session_factory) -> None:
    api = FakeAPI({'probe': [RuntimeError('boom')]})
    job_id = await make_job(session_factory, KIND_PROBE, {'target': 'x'}, [EU], ['mts|пфо|on'])
    await make_runner(session_factory, api, FakeClock()).run(job_id)
    job = await load(session_factory, job_id)
    assert (job.status, job.error_code, job.error_message) == (STATUS_FAILED, 'internal_error', 'boom')


async def test_run_skips_finished_and_missing_jobs(session_factory) -> None:
    api = FakeAPI()
    job_id = await make_job(session_factory, KIND_PROBE, {'target': 'x'}, [EU], ['mts|пфо|on'], status=STATUS_DONE)
    runner = make_runner(session_factory, api, FakeClock())
    await runner.run(job_id)
    await runner.run(job_id + 100)
    await runner.resume(job_id)
    assert api.calls == []


# ---------------------------------------------------------------- vless


async def test_vless_happy_path(session_factory) -> None:
    api = FakeAPI({'start_vless': [body('v1_submit')], 'get_vless': [body('v1_poll_00'), body('v1_poll_12')]})
    job_id = await make_job(
        session_factory,
        KIND_VLESS,
        load_bschek_fixture('v1_submit')['request'],
        [BS],
        ['tele2|цфо|on', 'dobro|цфо|on'],
    )
    await make_runner(session_factory, api, FakeClock()).run(job_id)

    job = await load(session_factory, job_id)
    assert (job.status, job.external_id, job.cost_kopeks, job.estimate_is_exact) == (STATUS_DONE, 43300, 206, True)
    assert (job.estimated_kopeks, job.units_effective) == (206, ['tele2|цфо|on', 'dobro|цфо|on'])
    assert [leg.verdict for leg in job.legs] == ['reachable', 'reachable']
    assert job.result['submit']['test_id'] == 43300 and job.result['status']['state'] == 'done'
    assert [call[0] for call in api.calls] == ['start_vless', 'get_vless', 'get_vless']


async def test_vless_over_cost_limit_is_cancelled_right_after_submit(session_factory) -> None:
    api = FakeAPI({'start_vless': [body('v1_submit')], 'cancel_vless': [body('v2_cancel')]})
    job_id = await make_job(session_factory, KIND_VLESS, {'raw_input': 'x'}, [BS], ['tele2|цфо|on'])
    await make_runner(session_factory, api, FakeClock(), cost_limit=100).run(job_id)

    job = await load(session_factory, job_id)
    assert (job.status, job.error_code, job.external_id) == (STATUS_FAILED, 'cost_limit_exceeded', 43300)
    assert (job.cost_kopeks, job.estimated_kopeks, job.estimate_is_exact) == (0, 206, False)
    assert [c[0] for c in api.calls] == ['start_vless', 'cancel_vless']


async def test_vless_busy_fails_fast_and_retryable(session_factory) -> None:
    api = FakeAPI(
        {'start_vless': [BschekAPIError(code='test_in_progress', message='busy', status=409, retryable=True)]}
    )
    job_id = await make_job(session_factory, KIND_VLESS, {'raw_input': 'x'}, [BS], ['tele2|цфо|on'])
    await make_runner(session_factory, api, FakeClock()).run(job_id)
    job = await load(session_factory, job_id)
    assert (job.status, job.error_code, job.retryable, job.external_id) == (
        STATUS_FAILED,
        'test_in_progress',
        True,
        None,
    )


async def test_vless_gateway_error_on_submit_is_replayed_with_same_key(session_factory) -> None:
    api = FakeAPI(
        {
            'start_vless': [
                BschekGatewayError(code='http_502', message='cf', status=502, retryable=True),
                body('v1_submit'),
            ],
            'get_vless': [body('v1_poll_12')],
        }
    )
    clock = FakeClock()
    job_id = await make_job(session_factory, KIND_VLESS, {'raw_input': 'x'}, [BS], ['tele2|цфо|on', 'dobro|цфо|on'])
    await make_runner(session_factory, api, clock).run(job_id)
    job = await load(session_factory, job_id)
    assert (job.status, job.external_id) == (STATUS_DONE, 43300)
    keys = [call[1][0] for call in api.calls if call[0] == 'start_vless']
    assert len(keys) == 2 and len(set(keys)) == 1


async def test_vless_not_found_state_fails_job(session_factory) -> None:
    api = FakeAPI({'start_vless': [body('v1_submit')], 'get_vless': [body('v_notfound')]})
    job_id = await make_job(session_factory, KIND_VLESS, {'raw_input': 'x'}, [BS], ['tele2|цфо|on'])
    await make_runner(session_factory, api, FakeClock()).run(job_id)
    job = await load(session_factory, job_id)
    assert (job.status, job.error_code) == (STATUS_FAILED, 'not_found')


async def test_vless_cancel_marks_phase_and_resume_finalizes_as_cancelled(session_factory) -> None:
    """Отмена только дёргает API и ставит фазу; финал ставит поллер/обходчик по GET."""
    api = FakeAPI({'cancel_vless': [body('vC_cancel')], 'get_vless': [body('vC_after_cancel')]})
    runner = make_runner(session_factory, api, FakeClock())
    job_id = await make_job(
        session_factory,
        KIND_VLESS,
        {'raw_input': 'x'},
        [EU],
        ['dobro|цфо|on'],
        status=STATUS_RUNNING,
        external_id=43306,
        result={'submit': body('vC_submit')},
        cost_kopeks=206,
    )

    async with session_factory() as db:
        job = await crud.get_job(db, job_id)
        await runner.cancel(db, job)
        await db.commit()
        assert job.phase == PHASE_CANCELLING
    assert [call[0] for call in api.calls] == ['cancel_vless']

    await runner.resume(job_id)

    job = await load(session_factory, job_id)
    assert job.status == STATUS_CANCELLED
    assert [leg.verdict for leg in job.legs] == ['cancelled']
    assert job.cost_kopeks == 0 and job.estimate_is_exact is False


async def test_vless_cancelled_before_any_leg_started_is_cancelled_by_phase(session_factory) -> None:
    """Незапущенные леги в результате отсутствуют: пустой результат при фазе cancelling — отмена."""
    empty_done = {**body('vC_after_cancel'), 'result': []}
    api = FakeAPI({'cancel_vless': [body('vC_cancel')], 'get_vless': [empty_done]})
    runner = make_runner(session_factory, api, FakeClock())
    job_id = await make_job(
        session_factory,
        KIND_VLESS,
        {'raw_input': 'x'},
        [EU],
        ['dobro|цфо|on'],
        status=STATUS_RUNNING,
        external_id=43306,
        result={'submit': body('vC_submit')},
        cost_kopeks=206,
    )
    async with session_factory() as db:
        await runner.cancel(db, await crud.get_job(db, job_id))
        await db.commit()
    await runner.resume(job_id)
    job = await load(session_factory, job_id)
    assert (job.status, job.cost_kopeks, job.legs) == (STATUS_CANCELLED, 0, [])


async def test_cancel_tolerates_test_already_finishing(session_factory) -> None:
    api = FakeAPI(
        {'cancel_vless': [BschekAPIError(code='cannot_cancel_running', message='late', status=409, retryable=True)]}
    )
    runner = make_runner(session_factory, api, FakeClock())
    job_id = await make_job(
        session_factory, KIND_VLESS, {'raw_input': 'x'}, [EU], ['dobro|цфо|on'], status=STATUS_RUNNING, external_id=1
    )
    async with session_factory() as db:
        job = await runner.cancel(db, await crud.get_job(db, job_id))
        assert job.phase == PHASE_CANCELLING


async def test_cancel_rejects_probe_and_finished_jobs(session_factory) -> None:
    runner = make_runner(session_factory, FakeAPI(), FakeClock())
    job_id = await make_job(session_factory, KIND_PROBE, {'target': 'x'}, [EU], ['mts|пфо|on'])
    async with session_factory() as db:
        job = await crud.get_job(db, job_id)
        with pytest.raises(JobNotCancellable):
            await runner.cancel(db, job)
        await crud.update_job(db, job, status=STATUS_DONE)
        with pytest.raises(JobNotCancellable):
            await runner.cancel(db, job)
    vless_id = await make_job(session_factory, KIND_VLESS, {'raw_input': 'x'}, [EU], ['dobro|цфо|on'])
    async with session_factory() as db:
        with pytest.raises(JobNotCancellable):
            await runner.cancel(db, await crud.get_job(db, vless_id))


# ---------------------------------------------------------------- scan


async def test_scan_happy_path(session_factory) -> None:
    api = FakeAPI({'start_scan': [body('s1_submit')], 'get_scan': [body('s1_poll_00'), body('s1_poll_03')]})
    job_id = await make_job(
        session_factory, KIND_SCAN, load_bschek_fixture('s1_submit')['request'], CIDR, ['dobro|цфо|on']
    )
    await make_runner(session_factory, api, FakeClock()).run(job_id)
    job = await load(session_factory, job_id)
    assert (job.status, job.external_id, job.cost_kopeks, job.units_effective) == (
        STATUS_DONE,
        5355,
        61,
        ['dobro|цфо|on'],
    )
    assert job.result['status']['result']['up_n'] == 0 and job.legs == []


async def test_scan_cancelled_state_from_get(session_factory) -> None:
    api = FakeAPI({'start_scan': [body('sB_submit')], 'get_scan': [body('sB_after_0')]})
    job_id = await make_job(session_factory, KIND_SCAN, {'cidr': '192.0.2.0/24'}, CIDR, ['*|цфо|on'])
    await make_runner(session_factory, api, FakeClock()).run(job_id)
    job = await load(session_factory, job_id)
    assert (job.status, job.cost_kopeks) == (STATUS_CANCELLED, 0)
    assert len(job.units_effective) == 6


async def test_scan_failed_state_propagates_error_and_retryable(session_factory) -> None:
    failed = {'scan_id': 5355, 'state': 'failed', 'result_ready': False, 'error': 'lte_unavailable', 'retryable': True}
    api = FakeAPI({'start_scan': [body('s1_submit')], 'get_scan': [failed]})
    job_id = await make_job(session_factory, KIND_SCAN, {'cidr': '192.0.2.0/24'}, CIDR, ['dobro|цфо|on'])
    await make_runner(session_factory, api, FakeClock()).run(job_id)
    job = await load(session_factory, job_id)
    assert (job.status, job.error_code, job.retryable) == (STATUS_FAILED, 'lte_unavailable', True)


async def test_scan_lost_on_service_side_fails_job(session_factory) -> None:
    api = FakeAPI(
        {
            'start_scan': [body('s1_submit')],
            'get_scan': [BschekAPIError(code='not_found', message='Скан не найден', status=404)],
        }
    )
    job_id = await make_job(session_factory, KIND_SCAN, {'cidr': '192.0.2.0/24'}, CIDR, ['dobro|цфо|on'])
    await make_runner(session_factory, api, FakeClock()).run(job_id)
    job = await load(session_factory, job_id)
    assert (job.status, job.error_code) == (STATUS_FAILED, 'not_found')


async def test_scan_cancel_calls_api(session_factory) -> None:
    api = FakeAPI({'cancel_scan': [{'scan_id': 5358, 'state': 'cancelled'}]})
    runner = make_runner(session_factory, api, FakeClock())
    job_id = await make_job(
        session_factory,
        KIND_SCAN,
        {'cidr': '192.0.2.0/24'},
        CIDR,
        ['*|цфо|on'],
        status=STATUS_RUNNING,
        external_id=5358,
    )
    async with session_factory() as db:
        await runner.cancel(db, await crud.get_job(db, job_id))
    assert api.calls == [('cancel_scan', (5358,))]


# ---------------------------------------------------------------- timeout + sweeper


async def test_poll_timeout_leaves_job_running_and_sweep_resumes_it(session_factory) -> None:
    running = body('s1_poll_00')
    api = FakeAPI({'start_scan': [body('s1_submit')], 'get_scan': [running, running, body('s1_poll_03')]})
    clock = FakeClock()
    cfg = RunnerConfig(scan_poll_interval=4.0, scan_timeout_base=5.0, scan_timeout_per_unit=0.0, sweep_min_age_sec=0.0)
    runner = make_runner(session_factory, api, clock, config=cfg)
    job_id = await make_job(session_factory, KIND_SCAN, {'cidr': '192.0.2.0/24'}, CIDR, ['dobro|цфо|on'])
    await runner.run(job_id)
    assert (await load(session_factory, job_id)).status == STATUS_RUNNING

    await runner.sweep()
    assert runner.is_active(job_id)
    await asyncio.gather(*runner._tasks.values())
    assert not runner.is_active(job_id)
    assert (await load(session_factory, job_id)).status == STATUS_DONE


async def test_sweep_skips_fresh_and_active_jobs(session_factory) -> None:
    api = FakeAPI()
    cfg = RunnerConfig(sweep_min_age_sec=3600.0)
    runner = make_runner(session_factory, api, FakeClock(), config=cfg)
    await make_job(
        session_factory,
        KIND_SCAN,
        {'cidr': '192.0.2.0/24'},
        CIDR,
        ['dobro|цфо|on'],
        status=STATUS_RUNNING,
        external_id=1,
    )
    await runner.sweep()
    assert runner._tasks == {} and api.calls == []


async def test_sweeper_loop_runs_until_stopped(session_factory) -> None:
    api = FakeAPI()
    clock = FakeClock()
    runner = make_runner(session_factory, api, clock, config=RunnerConfig(sweep_interval=7.0))
    sweeps = 0

    async def counting_sweep() -> None:
        nonlocal sweeps
        sweeps += 1
        if sweeps == 3:
            runner.stop()

    runner.sweep = counting_sweep  # type: ignore[method-assign]
    await runner.sweeper_loop()
    assert sweeps == 3 and clock.sleeps == [7.0, 7.0, 7.0]


async def test_spawn_tracks_task_until_done(session_factory) -> None:
    fx = load_bschek_fixture('p1_probe')
    api = FakeAPI({'probe': [fx['body']]})
    runner = make_runner(session_factory, api, FakeClock())
    job_id = await make_job(session_factory, KIND_PROBE, fx['request'], [EU], ['mts|пфо|on'])
    task = runner.spawn(job_id)
    assert runner.is_active(job_id)
    await asyncio.gather(task)
    assert not runner.is_active(job_id)
    assert (await load(session_factory, job_id)).status == STATUS_DONE


async def test_scan_progress_is_stored_while_running(session_factory) -> None:
    """Пока скан идёт, его progress из GET лежит в задаче — кабинет показывает, сколько адресов уже проверено."""
    running = {
        **body('s1_poll_00'),
        'progress': {'done_ips': 128, 'total_ips': 512, 'percent': 25, 'units_done': 0, 'units_total': 2},
    }
    api = FakeAPI({'start_scan': [body('s1_submit')], 'get_scan': [running, body('s1_poll_03')]})
    job_id = await make_job(session_factory, KIND_SCAN, {'cidr': '192.0.2.0/24'}, CIDR, ['dobro|цфо|on'])
    await make_runner(session_factory, api, FakeClock()).run(job_id)

    job = await load(session_factory, job_id)
    assert job.status == STATUS_DONE
    assert job.result['progress'] == running['progress']
