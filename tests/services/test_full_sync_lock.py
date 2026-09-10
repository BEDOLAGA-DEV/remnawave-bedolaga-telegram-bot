"""Полная синхронизация не запускается второй раз, пока идёт первая.

Проход «в панель» по нескольким тысячам подписок идёт от получаса; запрос из
кабинета отваливается по таймауту и показывает ошибку, оператор жмёт ещё раз — и
второй проход шёл параллельно первому, удваивая нагрузку на панель и ловя 429.
Замок стоял только на автосинхронизации по расписанию (2026-09-10).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.remnawave_sync_service import (
    FullSyncAlreadyRunning,
    RemnaWaveAutoSyncService,
    is_full_sync_running,
    perform_full_sync,
)


def _service(gate: asyncio.Event | None = None) -> SimpleNamespace:
    async def import_from_panel(session, scope):
        if gate is not None:
            await gate.wait()
        return {'created': 0, 'updated': 0, 'errors': 0, 'deleted': 0}

    return SimpleNamespace(
        is_configured=True,
        sync_users_from_panel=AsyncMock(side_effect=import_from_panel),
        sync_users_to_panel=AsyncMock(return_value={'created': 0, 'updated': 0, 'errors': 0}),
        get_all_squads=AsyncMock(return_value=[]),
    )


@pytest.mark.asyncio
async def test_second_full_sync_is_refused_while_first_runs():
    gate = asyncio.Event()
    first, second = _service(gate), _service()

    task = asyncio.create_task(perform_full_sync(AsyncMock(), first))
    await asyncio.sleep(0)  # первый проход вошёл в импорт и ждёт панель
    assert is_full_sync_running()

    with pytest.raises(FullSyncAlreadyRunning):
        await perform_full_sync(AsyncMock(), second)
    second.sync_users_from_panel.assert_not_awaited()

    gate.set()
    user_stats, _server_stats = await task
    assert 'to_panel' in user_stats
    assert not is_full_sync_running()


@pytest.mark.asyncio
async def test_lock_is_released_after_a_failure():
    failing = _service()
    failing.sync_users_from_panel = AsyncMock(side_effect=RuntimeError('panel down'))

    with pytest.raises(RuntimeError):
        await perform_full_sync(AsyncMock(), failing)

    assert not is_full_sync_running()


@pytest.mark.asyncio
async def test_scheduler_sees_a_manual_full_sync_as_running():
    gate = asyncio.Event()
    task = asyncio.create_task(perform_full_sync(AsyncMock(), _service(gate)))
    await asyncio.sleep(0)

    scheduler = RemnaWaveAutoSyncService(service_factory=lambda: SimpleNamespace(is_configured=True))
    assert scheduler.get_status().is_running
    assert await scheduler.run_sync_now(reason='manual') == {'started': False, 'reason': 'already_running'}

    gate.set()
    user_stats, _server_stats = await task
    assert 'to_panel' in user_stats
    assert not scheduler.get_status().is_running
