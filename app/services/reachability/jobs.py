"""Сервис задач BSCHEKER: фон, повторы тем же ключом, опрос, отмена, обходчик.

Состояния: pending → running(phase) → done | failed | cancelled. Фазы running:
submitting → waiting (probe идёт) / polling (VLESS, скан) / retrieving (probe оборвался,
забираем результат повтором ключа) / cancelling. Любой повтор к API — только с
``job.idempotency_key`` и ``job.request`` как есть: новый ключ = второе списание.
Статус асинхронной задачи читается только из GET; ответ на повторный submit — не статус.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud import reachability as crud
from app.database.models import ReachabilityBatch, ReachabilityJob
from app.external.bschek_api import BschekAPI, BschekAPIError, BschekGatewayError
from app.services.reachability.batches import batch_cost_kopeks, batch_status_from_jobs
from app.services.reachability.gate import PaidCallGate
from app.services.reachability.kinds import KIND_PROBE, KIND_VLESS
from app.services.reachability.legs import build_probe_legs, build_vless_legs, merge_skipped, partial_probe_progress
from app.services.reachability.pricing import credits_to_kopeks, format_rubles


logger = structlog.get_logger(__name__)

STATUS_PENDING, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED = crud.JOB_STATUSES
PHASE_SUBMITTING = 'submitting'
PHASE_WAITING = 'waiting'
PHASE_RETRIEVING = 'retrieving'
PHASE_POLLING = 'polling'
PHASE_CANCELLING = 'cancelling'

# Сервис временно не может принять запрос — повтор тем же ключом безопасен.
TRANSIENT_CODES = frozenset(
    {'worker_unavailable', 'scanner_unavailable', 'lte_unavailable', 'maintenance', 'bot_not_ready', 'no_alive_modems'}
)
# На аккаунте уже идёт тест/скан — повторять бессмысленно, админу нужен 409 словами.
BUSY_CODES = frozenset({'test_in_progress', 'scan_in_progress', 'busy', 'too_many_active'})
# Отменять уже нечего: итог возьмёт контрольный GET.
CANCEL_OK_CODES = frozenset({'cannot_cancel_running', 'not_running', 'not_found'})
# Пробу можно остановить, пока она идёт у API: ключ уже ушёл, результат ещё не пришёл.
PROBE_CANCELLABLE_PHASES = frozenset({'waiting', 'retrieving'})
_SCAN_PENDING_STATES = ('queued', 'running')
_NO_DPI_ON_MESSAGE = 'Под фильтр Белого списка не попала ни одна симка'

ApiCall = Callable[[BschekAPI], Awaitable[dict]]


@dataclass(frozen=True)
class RunnerConfig:
    probe_retrieve_fast_interval: float = 15.0
    probe_retrieve_fast_window: float = 120.0
    probe_retrieve_slow_interval: float = 30.0
    probe_retrieve_max: float = 1200.0
    # Старше этого проба не поднимается обходчиком, а падает с внятной причиной.
    probe_max_age_sec: float = 2700.0
    vless_poll_interval: float = 5.0
    vless_timeout_base: float = 300.0
    vless_timeout_per_leg: float = 180.0
    vless_timeout_cap: float = 2700.0
    scan_poll_interval: float = 4.0
    scan_timeout_base: float = 180.0
    scan_timeout_per_unit: float = 60.0
    scan_timeout_cap: float = 2400.0
    transient_retries: int = 3
    transient_default_wait: float = 60.0
    internal_error_replay_wait: float = 60.0
    sweep_interval: float = 60.0
    sweep_min_age_sec: float = 30.0
    # Пачка: не более стольких проб одновременно (лимит API на пробы с несколькими целями) и пауза между итерациями.
    batch_parallel: int = 3
    batch_poll_interval: float = 2.0


class JobNotCancellable(Exception):
    """Отменять нечего: синхронная проба, уже завершённая или ещё не отправленная задача."""


def _leg_cancelled(leg: dict) -> bool:
    return bool(leg.get('cancelled')) or leg.get('stage') == 'cancelled'


def _vless_cancelled_cost(job: ReachabilityJob, legs_raw: list[dict]) -> dict[str, Any]:
    """Цена отменённого теста: API точную сумму не отдаёт — оценка «цена лега × завершённые леги»."""
    submit = (job.result or {}).get('submit') or {}
    total_legs = int(submit.get('n_servers') or 0) * int(submit.get('n_modems') or 0)
    per_leg = round(job.cost_kopeks / total_legs) if job.cost_kopeks and total_legs else 0
    completed = sum(1 for leg in legs_raw if not _leg_cancelled(leg))
    return {'cost_kopeks': completed * per_leg, 'estimate_is_exact': False}


class JobRunner:
    def __init__(
        self,
        *,
        client_factory: Callable[[], BschekAPI],
        gate: PaidCallGate,
        session_factory: Callable[[], Any],
        cost_limit_kopeks: Callable[[], int],
        config: RunnerConfig | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client_factory = client_factory
        self._gate = gate
        self._session_factory = session_factory
        self._cost_limit = cost_limit_kopeks
        self.cfg = config or RunnerConfig()
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self._tasks: dict[int, asyncio.Task] = {}
        self._batch_tasks: dict[int, asyncio.Task] = {}
        self._running = False

    # ------------------------------------------------------------ фон

    def _track(self, job_id: int, coro: Coroutine[Any, Any, None]) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks[job_id] = task

        def _forget(done: asyncio.Task) -> None:
            if self._tasks.get(job_id) is done:
                self._tasks.pop(job_id, None)

        task.add_done_callback(_forget)
        return task

    def spawn(self, job_id: int) -> asyncio.Task:
        return self._track(job_id, self.run(job_id))

    def spawn_resume(self, job_id: int) -> asyncio.Task:
        return self._track(job_id, self.resume(job_id))

    def is_active(self, job_id: int) -> bool:
        task = self._tasks.get(job_id)
        return task is not None and not task.done()

    async def run(self, job_id: int) -> None:
        await self._guarded(job_id, self._start)

    async def resume(self, job_id: int) -> None:
        """Подхватить незавершённую задачу после таймаута опроса или перезапуска бота."""
        await self._guarded(job_id, self._continue)

    async def sweep(self) -> None:
        """Все незавершённые задачи старше порога без активного фона — на возобновление."""
        async with self._session_factory() as db:
            jobs = await crud.list_unfinished_jobs(db)
        threshold = self._now().timestamp() - self.cfg.sweep_min_age_sec
        for job in jobs:
            stamp = job.updated_at or job.created_at
            if self.is_active(job.id) or (stamp is not None and stamp.timestamp() > threshold):
                continue
            self.spawn_resume(job.id)
        async with self._session_factory() as db:
            batches = await crud.list_unfinished_batches(db)
        for batch in batches:
            if not self.is_batch_active(batch.id):
                self.spawn_batch(batch.id)

    async def sweeper_loop(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.sweep()
            except Exception:
                logger.exception('Обходчик задач проверки упал на итерации')
            await self._sleep(self.cfg.sweep_interval)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------ пачка

    def spawn_batch(self, batch_id: int) -> asyncio.Task:
        task = asyncio.create_task(self.run_batch(batch_id))
        self._batch_tasks[batch_id] = task

        def _forget(done: asyncio.Task) -> None:
            if self._batch_tasks.get(batch_id) is done:
                self._batch_tasks.pop(batch_id, None)

        task.add_done_callback(_forget)
        return task

    def is_batch_active(self, batch_id: int) -> bool:
        task = self._batch_tasks.get(batch_id)
        return task is not None and not task.done()

    async def run_batch(self, batch_id: int) -> None:
        """Гнать задачи пачки не более ``batch_parallel`` одновременно; когда все завершены — подвести итог."""
        while True:
            async with self._session_factory() as db:
                batch = await crud.get_batch(db, batch_id)
                if batch is None or batch.status in crud.TERMINAL_STATUSES:
                    return
                jobs = await crud.jobs_for_batch(db, batch_id)
                cancelling = batch.phase == PHASE_CANCELLING
                final = batch_status_from_jobs(jobs, cancelling=cancelling)
                if final is not None:
                    await crud.update_batch(
                        db,
                        batch,
                        status=final,
                        phase=None,
                        cost_kopeks=batch_cost_kopeks(jobs),
                        finished_at=self._now(),
                    )
                    await db.commit()
                    logger.info('Пачка проверок завершена', batch_id=batch_id, status=final)
                    return
                if batch.status == STATUS_PENDING:
                    await crud.update_batch(db, batch, status=STATUS_RUNNING, started_at=self._now())
                    await db.commit()
                self._dispatch_batch_jobs(jobs, cancelling=cancelling)
            pending = [task for task in self._tasks.values() if not task.done()]
            if pending:
                await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            await self._sleep(self.cfg.batch_poll_interval)

    def _dispatch_batch_jobs(self, jobs: list[ReachabilityJob], *, cancelling: bool) -> None:
        active = sum(1 for job in jobs if job.status == STATUS_RUNNING or self.is_active(job.id))
        for job in jobs:
            if job.status == STATUS_RUNNING and not self.is_active(job.id):
                self.spawn_resume(job.id)
            elif job.status == STATUS_PENDING and not cancelling and active < self.cfg.batch_parallel:
                self.spawn(job.id)
                active += 1

    async def cancel_batch(self, db: AsyncSession, batch: ReachabilityBatch) -> ReachabilityBatch:
        """Очередь гаснет сразу и бесплатно, идущие пробы останавливаются у API; итог подведёт драйвер."""
        if batch.status not in crud.ACTIVE_STATUSES:
            raise JobNotCancellable('Проверка уже завершена')
        await crud.update_batch(db, batch, phase=PHASE_CANCELLING)
        for job in await crud.jobs_for_batch(db, batch.id):
            if job.status == STATUS_PENDING:
                await crud.update_job(db, job, status=STATUS_CANCELLED, phase=None, finished_at=self._now())
            elif job.status == STATUS_RUNNING:
                with contextlib.suppress(JobNotCancellable):
                    await self.cancel(db, job)
        await db.commit()
        return batch

    # ------------------------------------------------------------ каркас

    async def _guarded(self, job_id: int, step: Callable[[AsyncSession, ReachabilityJob], Awaitable[None]]) -> None:
        async with self._session_factory() as db:
            job = await crud.get_job(db, job_id)
            if job is None or job.status in crud.TERMINAL_STATUSES:
                return
            try:
                await step(db, job)
            except BschekAPIError as exc:
                await self._fail(db, job, exc.code, exc.message, exc.retryable, last_request_id=exc.request_id)
            except Exception as exc:
                logger.exception('Задача проверки упала', job_id=job_id, kind=job.kind)
                await db.rollback()
                await self._fail(db, job, 'internal_error', str(exc)[:500], False)

    async def _start(self, db: AsyncSession, job: ReachabilityJob) -> None:
        if job.status == STATUS_PENDING:
            await self._update(db, job, status=STATUS_RUNNING, phase=PHASE_SUBMITTING, started_at=self._now())
        if job.kind == KIND_PROBE:
            await self._run_probe(db, job)
        else:
            await self._run_async_kind(db, job)

    async def _continue(self, db: AsyncSession, job: ReachabilityJob) -> None:
        if job.kind == KIND_PROBE:
            if job.status == STATUS_PENDING or job.phase == PHASE_SUBMITTING:
                await self._start(db, job)
                return
            if self._job_age(job) > self.cfg.probe_max_age_sec:
                await self._fail(db, job, 'probe_stalled', self._stalled_message(job), False)
                return
            result = await self._retrieve_probe(db, job)
            if result is not None:
                await self._finish_probe(db, job, result)
        elif job.external_id is None:
            await self._start(db, job)
        else:
            await self._poll(db, job)

    def _job_age(self, job: ReachabilityJob) -> float:
        started = job.started_at or job.created_at
        if started is None:
            return 0.0
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return (self._now() - started).total_seconds()

    def _stalled_message(self, job: ReachabilityJob) -> str:
        """Текст для людей, не для разработчиков: что случилось и куда идти; номер запроса — для поддержки."""
        trace = (job.result or {}).get('retrieve') or {}
        minutes = int(self.cfg.probe_max_age_sec // 60)
        request_id = trace.get('request_id') or '—'
        return (
            f'Сервис BSCHEKER не отдал результат за {minutes} минут. Проверка у него могла завершиться, '
            f'а деньги списаться — напишите в поддержку BSCHEKER и назовите номер запроса {request_id}.'
        )

    def _progress_fields(self, job: ReachabilityJob, exc: BschekAPIError) -> dict[str, Any]:
        """След ответа и частичный результат (если API его прислал) поверх прежнего result."""
        result = {**(job.result or {}), 'retrieve': self._trace(exc, job, self._now())}
        details = exc.details or {}
        if details.get('legs') or details.get('total'):
            result['partial'] = partial_probe_progress(details)
        return {'result': result}

    @staticmethod
    def _trace(exc: BschekAPIError, job: ReachabilityJob, now: datetime) -> dict[str, Any]:
        return {
            'code': exc.code,
            'status': exc.status,
            'message': (exc.message or '')[:200],
            'request_id': exc.request_id,
            'attempt': job.attempts or 0,
            'at': now.isoformat(),
        }

    async def _update(self, db: AsyncSession, job: ReachabilityJob, **fields: Any) -> None:
        await crud.update_job(db, job, **fields)
        await db.commit()

    async def _fail(
        self, db: AsyncSession, job: ReachabilityJob, code: str, message: str, retryable: bool | None, **extra: Any
    ) -> None:
        fields = {
            'status': STATUS_FAILED,
            'phase': None,
            'error_code': code,
            'error_message': message,
            'retryable': bool(retryable),
            'finished_at': self._now(),
            **{key: value for key, value in extra.items() if value is not None},
        }
        await self._update(db, job, **fields)

    async def _call(self, call: ApiCall, *, paid: bool) -> dict:
        async with self._client_factory() as api:
            if paid:
                return await self._gate.run(lambda: call(api))
            return await call(api)

    def _retry_wait(self, exc: BschekAPIError, attempt: int) -> float | None:
        if exc.code in TRANSIENT_CODES and attempt <= self.cfg.transient_retries:
            return exc.retry_after or self.cfg.transient_default_wait
        if exc.status == 500 and attempt == 1:
            return self.cfg.internal_error_replay_wait
        return None

    async def _paid(self, db: AsyncSession, job: ReachabilityJob, call: ApiCall, *, retry_gateway: bool) -> dict:
        """Платный POST с ключом задачи; временные сбои повторяются тем же ключом."""
        attempt = 0
        while True:
            attempt += 1
            await self._update(db, job, attempts=(job.attempts or 0) + 1)
            try:
                return await self._call(call, paid=True)
            except BschekGatewayError:
                if not retry_gateway or attempt > self.cfg.transient_retries:
                    raise
                await self._sleep(self.cfg.transient_default_wait)
            except BschekAPIError as exc:
                wait = self._retry_wait(exc, attempt)
                if wait is None:
                    raise
                await self._sleep(wait)

    # ------------------------------------------------------------ probe

    async def _run_probe(self, db: AsyncSession, job: ReachabilityJob) -> None:
        await self._update(db, job, phase=PHASE_WAITING)
        try:
            result = await self._paid(
                db, job, lambda api: api.probe(job.request, job.idempotency_key), retry_gateway=False
            )
        except BschekGatewayError:
            # 524/обрыв: проверка идёт и деньги списаны — результат забирается повтором ключа.
            result = await self._retrieve_probe(db, job)
        except BschekAPIError as exc:
            if exc.code != 'request_in_progress':
                raise
            await self._update(db, job, **self._progress_fields(job, exc))
            result = await self._retrieve_probe(db, job)
        if result is not None:
            await self._finish_probe(db, job, result)

    async def _retrieve_probe(self, db: AsyncSession, job: ReachabilityJob) -> dict | None:
        """Повтор тем же ключом: часто первые 2 минуты, потом реже; None — доберёт обходчик."""
        # Отмену мог поставить другой запрос, пока мы ждали API: фазу «отменяем» не затирать.
        await db.refresh(job, attribute_names=['phase'])
        if job.phase != PHASE_CANCELLING:
            await self._update(db, job, phase=PHASE_RETRIEVING)
        started = self._clock()
        while self._clock() - started < self.cfg.probe_retrieve_max:
            fast = self._clock() - started < self.cfg.probe_retrieve_fast_window
            await self._sleep(self.cfg.probe_retrieve_fast_interval if fast else self.cfg.probe_retrieve_slow_interval)
            await self._update(db, job, attempts=(job.attempts or 0) + 1)
            try:
                return await self._call(lambda api: api.probe(job.request, job.idempotency_key), paid=True)
            except BschekAPIError as exc:
                if not isinstance(exc, BschekGatewayError) and exc.code != 'request_in_progress':
                    raise
                logger.info(
                    'Повтор пробы тем же ключом без результата', job_id=job.id, **self._trace(exc, job, self._now())
                )
                await self._update(db, job, **self._progress_fields(job, exc))
        logger.warning('Результат пробы не получен за отведённое время, доберёт обходчик', job_id=job.id)
        return None

    async def _finish_probe(self, db: AsyncSession, job: ReachabilityJob, result: dict) -> None:
        skipped = merge_skipped(job.skipped, result)
        if result.get('outcome') == 'no_dpi_on':
            await self._fail(
                db, job, 'no_dpi_on', _NO_DPI_ON_MESSAGE, False, result={'response': result}, skipped=skipped
            )
            return
        legs = build_probe_legs(job.targets or [], job.request or {}, result, checked_at=self._now())
        await crud.replace_legs(db, job.id, legs)
        # Отмену ставит другой запрос, пока этот ждёт ответа API: фазу перечитываем из базы.
        await db.refresh(job, attribute_names=['phase'])
        cancelled = job.phase == PHASE_CANCELLING
        await self._update(
            db,
            job,
            status=STATUS_CANCELLED if cancelled else STATUS_DONE,
            phase=None,
            result={'response': result},
            cost_kopeks=credits_to_kopeks(result.get('cost_credits')),
            refunded_kopeks=credits_to_kopeks(result.get('refunded')),
            units_effective=list(result.get('operators') or []),
            skipped=skipped,
            finished_at=self._now(),
        )

    # ------------------------------------------------------------ vless / scan

    @staticmethod
    def _submit(api: BschekAPI, job: ReachabilityJob) -> Awaitable[dict]:
        if job.kind == KIND_VLESS:
            return api.start_vless(job.request, job.idempotency_key)
        return api.start_scan(job.request, job.idempotency_key)

    @staticmethod
    def _submit_fields(job: ReachabilityJob, submit: dict, external_id: int) -> dict[str, Any]:
        fields: dict[str, Any] = {
            'external_id': external_id,
            'phase': PHASE_POLLING,
            'result': {'submit': submit},
            'skipped': merge_skipped(job.skipped, submit),
        }
        if job.kind != KIND_VLESS:
            return {**fields, 'units_effective': [unit.get('op_key') for unit in submit.get('units') or []]}
        cost = credits_to_kopeks(submit.get('cost_credits'))
        return {
            **fields,
            'cost_kopeks': cost,
            'estimated_kopeks': cost,
            'estimate_is_exact': True,
            'units_effective': list(job.units_resolved or []),
        }

    async def _run_async_kind(self, db: AsyncSession, job: ReachabilityJob) -> None:
        submit = await self._paid(db, job, lambda api: self._submit(api, job), retry_gateway=True)
        if submit.get('outcome') == 'no_dpi_on':
            await self._fail(db, job, 'no_dpi_on', _NO_DPI_ON_MESSAGE, False, result={'submit': submit})
            return
        external_id = submit.get('test_id' if job.kind == KIND_VLESS else 'scan_id')
        if external_id is None:
            message = 'API не вернул идентификатор задачи'
            await self._fail(db, job, 'unexpected_response', message, False, result={'submit': submit})
            return
        await self._update(db, job, **self._submit_fields(job, submit, int(external_id)))
        if job.kind == KIND_VLESS and await self._cancel_if_over_limit(db, job):
            return
        await self._poll(db, job)

    async def _cancel_if_over_limit(self, db: AsyncSession, job: ReachabilityJob) -> bool:
        """Точная цена известна только после постановки: выше потолка — отменить сразу, пока леги не пошли."""
        limit, cost = self._cost_limit(), job.cost_kopeks
        if not limit or not cost or cost <= limit:
            return False
        await self._try_cancel_remote(job)
        message = (
            f'Цена теста {format_rubles(cost)} выше потолка {format_rubles(limit)}; тест отменён сразу, списания нет'
        )
        await self._fail(db, job, 'cost_limit_exceeded', message, False, cost_kopeks=0, estimate_is_exact=False)
        return True

    def _timeout_for(self, job: ReachabilityJob) -> float:
        units = max(1, len(job.units_effective or job.units_resolved or []))
        if job.kind == KIND_VLESS:
            legs = max(1, len(job.targets or [])) * units
            return min(self.cfg.vless_timeout_cap, self.cfg.vless_timeout_base + self.cfg.vless_timeout_per_leg * legs)
        return min(self.cfg.scan_timeout_cap, self.cfg.scan_timeout_base + self.cfg.scan_timeout_per_unit * units)

    def _status_call(self, job: ReachabilityJob) -> ApiCall:
        external_id = int(job.external_id)
        if job.kind == KIND_VLESS:
            return lambda api: api.get_vless(external_id)
        return lambda api: api.get_scan(external_id)

    async def _poll(self, db: AsyncSession, job: ReachabilityJob) -> None:
        interval = self.cfg.vless_poll_interval if job.kind == KIND_VLESS else self.cfg.scan_poll_interval
        deadline = self._clock() + self._timeout_for(job)
        while self._clock() < deadline:
            await self._sleep(interval)
            try:
                status = await self._call(self._status_call(job), paid=False)
            except BschekGatewayError:
                continue
            except BschekAPIError as exc:
                if exc.code == 'not_found' or exc.status == 404:
                    await self._fail(db, job, 'not_found', 'Задача пропала на стороне сервиса', False)
                    return
                raise
            handler = self._handle_vless_status if job.kind == KIND_VLESS else self._handle_scan_status
            if await handler(db, job, status):
                return
        logger.warning('Опрос задачи проверки исчерпал таймаут, доберёт обходчик', job_id=job.id, kind=job.kind)

    async def _handle_vless_status(self, db: AsyncSession, job: ReachabilityJob, status: dict) -> bool:
        state = status.get('state')
        merged = {**(job.result or {}), 'status': status}
        if state == 'not_found':
            await self._fail(db, job, 'not_found', 'Тест пропал на стороне сервиса', False, result=merged)
            return True
        if not (status.get('result_ready') or state in ('done', 'cancelled')):
            return False
        legs_raw = [leg for leg in (status.get('result') or []) if isinstance(leg, dict)]
        await crud.replace_legs(db, job.id, build_vless_legs(job.targets or [], legs_raw, checked_at=self._now()))
        # Незапущенные леги в результате отсутствуют: пустой результат после нашей отмены — тоже отмена.
        await db.refresh(job, attribute_names=['phase'])
        cancelled = (
            state == 'cancelled'
            or any(_leg_cancelled(leg) for leg in legs_raw)
            or (job.phase == PHASE_CANCELLING and not legs_raw)
        )
        fields: dict[str, Any] = {
            'status': STATUS_CANCELLED if cancelled else STATUS_DONE,
            'phase': None,
            'result': merged,
            'finished_at': self._now(),
        }
        if cancelled:
            fields = {**fields, **_vless_cancelled_cost(job, legs_raw)}
        await self._update(db, job, **fields)
        return True

    async def _handle_scan_status(self, db: AsyncSession, job: ReachabilityJob, status: dict) -> bool:
        state = status.get('state')
        if state in _SCAN_PENDING_STATES:
            progress = status.get('progress')
            if isinstance(progress, dict) and progress != (job.result or {}).get('progress'):
                await self._update(db, job, result={**(job.result or {}), 'progress': progress})
            return False
        merged = {**(job.result or {}), 'status': status}
        if state == 'failed':
            code = str(status.get('error') or 'scan_failed')
            message = 'Скан завершился ошибкой на стороне сервиса'
            await self._fail(db, job, code, message, bool(status.get('retryable')), result=merged)
            return True
        result = status.get('result') or {}
        await self._update(
            db,
            job,
            status=STATUS_CANCELLED if state == 'cancelled' else STATUS_DONE,
            phase=None,
            result=merged,
            cost_kopeks=credits_to_kopeks(result.get('cost_credits')),
            finished_at=self._now(),
        )
        return True

    # ------------------------------------------------------------ отмена

    async def _try_cancel_remote(self, job: ReachabilityJob) -> None:
        """Отмена у API: пробу — её ключом, тест и скан — их идентификатором. «Уже нечего» — не ошибка."""

        def call(api: BschekAPI) -> Awaitable[dict]:
            if job.kind == KIND_PROBE:
                return api.cancel_probe(job.idempotency_key)
            external_id = int(job.external_id)
            return api.cancel_vless(external_id) if job.kind == KIND_VLESS else api.cancel_scan(external_id)

        try:
            await self._call(call, paid=False)
        except BschekAPIError as exc:
            if exc.code not in CANCEL_OK_CODES and exc.status != 404:
                raise

    async def cancel(self, db: AsyncSession, job: ReachabilityJob) -> ReachabilityJob:
        """Дёрнуть отмену у API и пометить фазу; итог (статус, цена) поставит поллер или обходчик.

        Проба: висящий POST вернётся сам с тем, что успели измерить, платим только за это.
        """
        if job.status not in crud.ACTIVE_STATUSES:
            raise JobNotCancellable('Задача уже завершена')
        if job.kind == KIND_PROBE:
            if job.phase not in PROBE_CANCELLABLE_PHASES:
                raise JobNotCancellable('Проверка ещё не отправлена, подождите пару секунд')
        elif job.external_id is None:
            raise JobNotCancellable('Задача ещё не отправлена, подождите пару секунд')
        await self._update(db, job, phase=PHASE_CANCELLING)
        await self._try_cancel_remote(job)
        return job
