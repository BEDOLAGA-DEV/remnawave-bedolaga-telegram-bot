"""Collect short message bursts and process one request at a time per chat."""

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field


@dataclass
class _Entry:
    message: object
    arrived: float
    done: asyncio.Future


@dataclass
class _State:
    entries: list[_Entry] = field(default_factory=list)
    task: asyncio.Task | None = None
    last_started: float = float('-inf')


class MessageQueue:
    def __init__(self, quiet_seconds: float = 1.5, max_wait: float = 4, batch_size: int = 10, pending_limit: int = 50):
        self.quiet_seconds = quiet_seconds
        self.max_wait = max_wait
        self.batch_size = batch_size
        self.pending_limit = pending_limit
        self._states: dict[Hashable, _State] = {}
        self._last_started: dict[Hashable, float] = {}

    async def submit(
        self, key: Hashable, message: object, process: Callable[[list], Awaitable[None]], interval: float = 0
    ):
        state = self._states.setdefault(key, _State(last_started=self._last_started.get(key, float('-inf'))))
        if len(state.entries) >= self.pending_limit:
            raise OverflowError('Message queue is full')
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        state.entries.append(_Entry(message, loop.time(), done))
        if state.task is None:
            state.task = asyncio.create_task(self._run(key, state, process, max(0, interval)))
        await done

    async def _run(self, key, state, process, interval):
        loop = asyncio.get_running_loop()
        batch = []
        try:
            while state.entries:
                while True:
                    deadline = min(
                        state.entries[-1].arrived + self.quiet_seconds, state.entries[0].arrived + self.max_wait
                    )
                    delay = max(deadline, state.last_started + interval) - loop.time()
                    if delay <= 0:
                        break
                    await asyncio.sleep(delay)
                batch, state.entries = state.entries[: self.batch_size], state.entries[self.batch_size :]
                state.last_started = loop.time()
                self._last_started[key] = state.last_started
                if len(self._last_started) > 10000:
                    self._last_started = {k: t for k, t in self._last_started.items() if loop.time() - t < 600}
                try:
                    await process([entry.message for entry in batch])
                except Exception as error:
                    for entry in batch:
                        if not entry.done.done():
                            entry.done.set_exception(error)
                else:
                    for entry in batch:
                        if not entry.done.done():
                            entry.done.set_result(None)
                batch = []
        finally:
            for entry in batch + state.entries:
                if not entry.done.done():
                    entry.done.cancel()
            self._states.pop(key, None)

    async def close(self):
        states = list(self._states.values())
        tasks = [state.task for state in states if state.task is not None]
        for state in states:
            for entry in state.entries:
                if not entry.done.done():
                    entry.done.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._states.clear()
