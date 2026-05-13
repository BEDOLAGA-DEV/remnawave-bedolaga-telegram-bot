"""Shared fire-and-forget task helper with strong-ref tracking."""

from __future__ import annotations

import asyncio

import structlog


logger = structlog.get_logger(__name__)

# Module-level set keeps strong references to fire-and-forget tasks so the
# event loop's weak-ref policy doesn't garbage-collect them mid-flight.
_background_tasks: set[asyncio.Task] = set()


def spawn_bg(coro) -> asyncio.Task | None:
    """Schedule a coroutine without blocking the caller.

    Tracks the task to prevent GC. Logs uncaught exceptions on completion.
    Does NOT gate on any feature flag — it's a pure scheduler.
    """
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        # No running event loop (e.g. import-time call): close coro to silence
        # "coroutine was never awaited" warnings.
        try:
            coro.close()
        except Exception:
            pass
        return None
    _background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        exc = t.exception()
        if exc:
            logger.warning('background_task_failed', error=str(exc))

    task.add_done_callback(_done)
    return task


async def drain_background_tasks(timeout: float = 5.0) -> None:
    """Wait for in-flight tasks on shutdown. Used by signal handlers."""
    if not _background_tasks:
        return
    await asyncio.wait(set(_background_tasks), timeout=timeout)
