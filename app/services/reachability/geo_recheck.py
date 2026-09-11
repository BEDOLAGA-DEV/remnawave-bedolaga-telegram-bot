"""Повтор проваленного города из отчёта GEO — в тот же тест.

Кнопки «Тот же IP» / «Сменить IP» не заводят новую задачу и не растят историю: сервис
гоняет свой прогон на один город, бот ждёт его фоном и вливает строки и деньги в отчёт
родителя. Пока прогон идёт, запись лежит в ``result.rechecks[ключ]``; итог убирает её,
сбой — оставляет с причиной словами. Записи без живой фоновой задачи (перезапуск бота)
гасит чтение задачи сервисом (``expire_rechecks``).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol

import structlog

from app.database.crud import reachability as crud
from app.database.models import ReachabilityJob
from app.external.bschek_api import BschekAPI, BschekAPIError, BschekGatewayError
from app.services.reachability.geo_messages import geo_error_message
from app.services.reachability.geo_result import (
    merge_recheck,
    normalize_rows,
    recheck_failed,
    recheck_finished,
    recheck_money,
    recheck_updated,
)
from app.services.reachability.pricing import credits_to_kopeks


logger = structlog.get_logger(__name__)

ApiCall = Callable[[BschekAPI], Awaitable[dict]]
Fields = dict[str, Any]

_TERMINAL_STATES = ('done', 'empty', 'error', 'aborted')
VANISHED_MESSAGE = 'Прогон пропал на стороне сервиса'
GATEWAY_MESSAGE = 'Сервис BSCHEKER не отвечает — повтор не удался'
NO_RUN_ID_MESSAGE = 'Сервис не вернул идентификатор прогона'
#: Окно между записью повтора в отчёт и стартом фоновой задачи: моложе этого запись — не сирота.
RECHECK_STALE_GRACE_SEC = 60.0


class GeoTimeouts(Protocol):
    geo_poll_interval: float
    geo_timeout_base: float
    geo_timeout_factor: float
    geo_timeout_cap: float
    transient_retries: int
    transient_default_wait: float


def geo_timeout(cfg: GeoTimeouts, estimated_sec: float) -> float:
    """Сколько ждать прогон GEO: база плюс прогноз сервиса с запасом, не дольше потолка."""
    return min(cfg.geo_timeout_cap, cfg.geo_timeout_base + cfg.geo_timeout_factor * estimated_sec)


def _timeout_message(minutes: int, run_id: int) -> str:
    return (
        f'Сервис не отдал итог повтора за {minutes} мин — прогон {run_id} мог завершиться и списать деньги; '
        'назовите его номер в поддержке BSCHEKER'
    )


class GeoRecheckRunner:
    """Фоновые повторы городов по родительским отчётам: запуск, опрос, слияние — под замком на родителя."""

    def __init__(
        self,
        *,
        call: Callable[..., Awaitable[dict]],
        retry_wait: Callable[[BschekAPIError, int], float | None],
        session_factory: Callable[[], Any],
        cfg: GeoTimeouts,
        sleep: Callable[[float], Awaitable[None]],
        clock: Callable[[], float],
        now: Callable[[], datetime],
        geo_names: Callable[[], Awaitable[dict]] | None,
    ) -> None:
        self._call = call
        self._retry_wait = retry_wait
        self._session_factory = session_factory
        self._cfg = cfg
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self._geo_names = geo_names
        self._tasks: dict[tuple[int, str], asyncio.Task] = {}
        # Два повтора на одном отчёте пишут в один JSON: чтение-правка-запись только по очереди.
        self._locks: dict[int, asyncio.Lock] = {}

    # ------------------------------------------------------------ фон

    def spawn(self, parent_id: int, key: str, *, request: dict, reserve_kopeks: int) -> asyncio.Task:
        task = asyncio.create_task(self.run(parent_id, key, request=request, reserve_kopeks=reserve_kopeks))
        self._tasks[(parent_id, key)] = task

        def _forget(done: asyncio.Task) -> None:
            if self._tasks.get((parent_id, key)) is done:
                self._tasks.pop((parent_id, key), None)
            if not any(pid == parent_id for pid, _ in self._tasks):
                self._locks.pop(parent_id, None)

        task.add_done_callback(_forget)
        return task

    def is_active(self, parent_id: int, key: str) -> bool:
        task = self._tasks.get((parent_id, key))
        return task is not None and not task.done()

    async def run(self, parent_id: int, key: str, *, request: dict, reserve_kopeks: int) -> None:
        try:
            await self._run(parent_id, key, request=request, reserve_kopeks=reserve_kopeks)
        except BschekGatewayError:
            await self._fail(parent_id, key, GATEWAY_MESSAGE)
        except BschekAPIError as exc:
            await self._fail(parent_id, key, geo_error_message(exc))
        except Exception as exc:
            logger.exception('Повтор города GEO упал', parent_id=parent_id, key=key)
            await self._fail(parent_id, key, str(exc)[:200])

    async def _run(self, parent_id: int, key: str, *, request: dict, reserve_kopeks: int) -> None:
        submit = await self._start(request)
        raw_run_id = submit.get('run_id')
        if raw_run_id is None:
            await self._fail(parent_id, key, NO_RUN_ID_MESSAGE)
            return
        run_id = int(raw_run_id)
        # Резерв из ответа на запуск точнее расчёта.
        from_submit = credits_to_kopeks(submit.get('reserve_credits'))
        reserve = reserve_kopeks if from_submit is None else from_submit
        await self._write(
            parent_id,
            lambda job: {'result': recheck_updated(job.result or {}, key, run_id=run_id, reserve_kopeks=reserve)},
        )
        estimated = float(submit.get('estimated_sec') or 0)
        status = await self._poll(run_id, estimated)
        if status is None:
            minutes = int(geo_timeout(self._cfg, estimated) // 60)
            await self._fail(parent_id, key, _timeout_message(minutes, run_id))
            return
        if status.get('state') == 'error':
            message = str(status.get('error') or 'Прогон завершился ошибкой на стороне сервиса')
            await self._fail(parent_id, key, message)
            return
        names = await self._geo_names() if self._geo_names else {}
        rows = normalize_rows(list(status.get('rows') or []), names)
        charged = credits_to_kopeks(status.get('charged_credits')) or 0

        def merged(job: ReachabilityJob) -> Fields:
            result = merge_recheck(job.result or {}, rows, run_id=run_id)
            money = recheck_money(job, reserve_kopeks=reserve, charged_kopeks=charged)
            return {**money, 'result': recheck_finished(result, key)}

        await self._write(parent_id, merged)
        logger.info(
            'Повтор города GEO влит в отчёт',
            parent_id=parent_id,
            key=key,
            run_id=run_id,
            rows=len(rows),
            charged_kopeks=charged,
        )

    # ------------------------------------------------------------ API

    async def _start(self, request: dict) -> dict:
        """Платный запуск со своим ключом идемпотентности; временные сбои повторяются тем же ключом."""
        idempotency_key = str(uuid.uuid4())
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._call(lambda api: api.geo_start(request, idempotency_key), paid=True)
            except BschekGatewayError:
                if attempt > self._cfg.transient_retries:
                    raise
                await self._sleep(self._cfg.transient_default_wait)
            except BschekAPIError as exc:
                wait = self._retry_wait(exc, attempt)
                if wait is None:
                    raise
                await self._sleep(wait)

    async def _poll(self, run_id: int, estimated_sec: float) -> dict | None:
        """Опрос до терминала; пропавший прогон — терминал с ошибкой словами; таймаут — None."""
        deadline = self._clock() + geo_timeout(self._cfg, estimated_sec)
        while self._clock() < deadline:
            await self._sleep(self._cfg.geo_poll_interval)
            try:
                status = await self._call(lambda api: api.geo_run(run_id), paid=False)
            except BschekGatewayError:
                continue
            except BschekAPIError as exc:
                if exc.code == 'not_found' or exc.status == 404:
                    return {'state': 'error', 'error': VANISHED_MESSAGE}
                raise
            if status.get('state') in _TERMINAL_STATES:
                return status
        return None

    # ------------------------------------------------------------ запись в родителя

    def _lock(self, parent_id: int) -> asyncio.Lock:
        return self._locks.setdefault(parent_id, asyncio.Lock())

    async def _write(self, parent_id: int, fields_of: Callable[[ReachabilityJob], Fields]) -> None:
        async with self._lock(parent_id), self._session_factory() as db:
            job = await crud.get_job(db, parent_id)
            if job is None:
                logger.warning('Родитель повтора GEO не найден', parent_id=parent_id)
                return
            await crud.update_job(db, job, **fields_of(job))
            await db.commit()

    async def _fail(self, parent_id: int, key: str, message: str) -> None:
        finished_at = self._now().isoformat()
        await self._write(
            parent_id, lambda job: {'result': recheck_failed(job.result or {}, key, message, finished_at=finished_at)}
        )
        logger.warning('Повтор города GEO не удался', parent_id=parent_id, key=key, reason=message)
