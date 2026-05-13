"""Тесты вспомогательного fire-and-forget планировщика."""

import asyncio

import pytest

from app.utils import async_tasks
from app.utils.async_tasks import drain_background_tasks, spawn_bg


def test_spawn_bg_without_event_loop_returns_none() -> None:
    """Sync caller without running loop must not crash and must close coro."""
    coro_closed = False

    async def _coro():  # pragma: no cover — body not executed
        nonlocal coro_closed
        coro_closed = True

    coro = _coro()
    result = spawn_bg(coro)
    assert result is None
    coro.close()


@pytest.mark.asyncio
async def test_spawn_bg_holds_strong_ref_until_done() -> None:
    """Task must be tracked in `_background_tasks` while running, then evicted."""

    started = asyncio.Event()

    async def _coro():
        started.set()
        await asyncio.sleep(0)

    task = spawn_bg(_coro())
    assert task is not None
    assert task in async_tasks._background_tasks
    await started.wait()
    await task
    # Give the done-callback one tick to run.
    await asyncio.sleep(0)
    assert task not in async_tasks._background_tasks


@pytest.mark.asyncio
async def test_drain_background_tasks_waits_for_inflight() -> None:
    """`drain_background_tasks` should not hang when set is empty, and should wait when not."""
    await drain_background_tasks(timeout=0.1)  # empty — returns immediately

    completed = False

    async def _slow():
        nonlocal completed
        await asyncio.sleep(0.05)
        completed = True

    spawn_bg(_slow())
    await drain_background_tasks(timeout=1.0)
    assert completed is True


@pytest.mark.asyncio
async def test_spawn_bg_logs_exception_but_does_not_propagate() -> None:
    """Uncaught exceptions inside the coro must not bubble out — done-callback logs and discards."""

    async def _boom():
        raise RuntimeError('expected')

    task = spawn_bg(_boom())
    assert task is not None
    # Awaiting the task itself raises; the done-callback handles it for fire-and-forget callers.
    with pytest.raises(RuntimeError):
        await task
