"""Шлюз платных POST-вызовов bschekbot: не чаще 1 запроса в секунду на аккаунт.

429 гасится здесь же: пауза по retry_after и повтор ТОГО ЖЕ вызова (тот же
Idempotency-Key внутри), админ этого не видит. Замок держится только пока
считается пауза — сам вызов может идти минуты.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from app.external.bschek_api import BschekAPIError


class PaidCallGate:
    def __init__(
        self,
        *,
        min_interval: float = 1.1,
        max_rate_limit_retries: int = 5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._min_interval = min_interval
        self._max_retries = max_rate_limit_retries
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_start: float | None = None

    async def _wait_for_slot(self) -> None:
        async with self._lock:
            now = self._clock()
            if self._last_start is not None:
                pause = self._last_start + self._min_interval - now
                if pause > 0:
                    await self._sleep(pause)
                    now = self._clock()
            self._last_start = now

    async def run[T](self, call: Callable[[], Awaitable[T]]) -> T:
        attempt = 0
        while True:
            await self._wait_for_slot()
            try:
                return await call()
            except BschekAPIError as exc:
                if exc.code != 'rate_limited' or attempt >= self._max_retries:
                    raise
                attempt += 1
                await self._sleep(exc.retry_after or 1.0)
