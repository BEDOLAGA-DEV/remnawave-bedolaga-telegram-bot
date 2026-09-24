"""Обходчик мониторов DPI//CHECKER: итог прогона → одно уведомление админам.

Сервис шлёт ``monitor.run`` в НАЧАЛЕ прогона и не сообщает о его завершении (разведка 2026-09-24),
поэтому итог забираем сами: последний завершённый прогон своих мониторов → проверка → вид → решение,
сообщать ли. Вебхук лишь будит обход раньше срока; без внешнего адреса бота уведомления тоже приходят.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from app.database.crud import dpichecker as crud
from app.external.dpichecker_api import DpiCheckerAPIError
from app.services.dpichecker.notify import monitor_run_text
from app.services.dpichecker.presenter import present_check


logger = structlog.get_logger(__name__)

SWEEP_INTERVAL_SEC = 300
RUNS_TO_LOOK = 5
FINISHED_RUN = frozenset({'completed', 'failed'})
DOWN_STATUSES = frozenset({'down', 'failing', 'alert'})


def should_notify(monitor: dict[str, Any], run: dict[str, Any], previous_status: str | None) -> bool:
    """Каждый прогон — если «сообщать и об успехе»; иначе тревога (неудач подряд ≥ порога) и восстановление."""
    if run.get('check_status') not in FINISHED_RUN:
        return False
    if monitor.get('notify_on_success'):
        return True
    fails = int(monitor.get('consecutive_fails') or 0)
    if fails >= int(monitor.get('alert_after_fails') or 1):
        return True
    return fails == 0 and previous_status in DOWN_STATUSES


class MonitorWatch:
    def __init__(
        self,
        *,
        api_factory: Callable[[], Any],
        session_factory: Callable[[], Any],
        notify: Callable[[str], Awaitable[Any]],
        cabinet_url: Callable[[], str | None] = lambda: None,
        sleep_interval: float = SWEEP_INTERVAL_SEC,
    ) -> None:
        self._api_factory = api_factory
        self._session_factory = session_factory
        self._notify = notify
        self._cabinet_url = cabinet_url
        self._interval = sleep_interval
        self._wake = asyncio.Event()
        self._running = False

    def poke(self, monitor_id: int | None = None) -> None:
        """Прогон начался — проверить раньше срока (итог придёт через минуты)."""
        self._wake.set()

    async def sweep(self) -> int:
        sent = 0
        async with self._session_factory() as db:
            # Номера — заранее: после отката по сбою одного монитора объекты сессии истекают.
            action_ids = [action.id for action in await crud.list_monitors(db)]
            for action_id in action_ids:
                try:
                    action = await crud.get_action(db, action_id)
                    if action is not None:
                        sent += await self._check_one(db, action)
                except Exception as error:
                    await db.rollback()
                    logger.warning(
                        'DPI//CHECKER: обход монитора не удался', action_id=action_id, error=str(error)[:200]
                    )
        return sent

    async def _check_one(self, db: Any, action: Any) -> int:
        async with self._api_factory() as api:
            try:
                monitor = await api.get_monitor(action.remote_id)
            except DpiCheckerAPIError as exc:
                if exc.code == 'not_found':
                    action.status = 'deleted'
                    await db.commit()
                    return 0
                raise
            runs = (await api.monitor_runs(action.remote_id, limit=RUNS_TO_LOOK)).get('items') or []
            fresh = [
                run
                for run in runs
                if run.get('check_status') in FINISHED_RUN and int(run['id']) > (action.last_run_id or 0)
            ]
            if not fresh:
                return 0
            run = max(fresh, key=lambda item: int(item['id']))
            check = await api.get_check(run['check_id']) if run.get('check_id') else {'results': []}
        previous = action.status
        notify = should_notify(monitor, run, previous)
        action.last_run_id = int(run['id'])
        action.status = str(monitor.get('last_status') or previous)
        await db.commit()
        if not notify:
            return 0
        names = {str(t.get('value')): str(t.get('name') or '') for t in action.targets or []}
        await self._notify(
            monitor_run_text(action, monitor, present_check(check, names), cabinet_url=self._cabinet_url())
        )
        return 1

    async def loop(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.sweep()
            except Exception:
                logger.exception('Обходчик мониторов DPI//CHECKER упал на итерации')
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._running = False
        self._wake.set()
