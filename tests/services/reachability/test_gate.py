"""Шлюз платных вызовов: 1 запрос в секунду на аккаунт, 429 повторяется тем же вызовом.

Замок не держится на время самого вызова: проба по всему флоту идёт минуты, и
она не должна блокировать запуск VLESS.
"""

from __future__ import annotations

import asyncio

import pytest

from app.external.bschek_api import BschekAPIError
from app.services.reachability.gate import PaidCallGate


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def test_spaces_calls_by_min_interval() -> None:
    clock = FakeClock()
    gate = PaidCallGate(min_interval=1.1, clock=clock, sleep=clock.sleep)
    starts: list[float] = []

    async def call() -> str:
        starts.append(clock.now)
        return 'ok'

    assert await gate.run(call) == 'ok'
    assert await gate.run(call) == 'ok'
    assert starts == [0.0, 1.1]


async def test_retries_rate_limited_with_retry_after_and_same_call() -> None:
    clock = FakeClock()
    gate = PaidCallGate(min_interval=1.1, clock=clock, sleep=clock.sleep)
    attempts = 0

    async def call() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise BschekAPIError(code='rate_limited', message='slow down', status=429, retryable=True, retry_after=0.98)
        return 'done'

    assert await gate.run(call) == 'done'
    assert attempts == 2
    assert 0.98 in clock.sleeps


async def test_gives_up_after_max_rate_limit_retries() -> None:
    clock = FakeClock()
    gate = PaidCallGate(min_interval=0, max_rate_limit_retries=2, clock=clock, sleep=clock.sleep)

    async def call() -> str:
        raise BschekAPIError(code='rate_limited', message='x', status=429, retryable=True, retry_after=None)

    with pytest.raises(BschekAPIError) as exc:
        await gate.run(call)
    assert exc.value.code == 'rate_limited'


async def test_other_errors_pass_through_immediately() -> None:
    clock = FakeClock()
    gate = PaidCallGate(clock=clock, sleep=clock.sleep)

    async def call() -> str:
        raise BschekAPIError(code='no_dpi_on', message='x', status=400)

    with pytest.raises(BschekAPIError):
        await gate.run(call)
    assert clock.sleeps == []


async def test_lock_is_not_held_during_the_call() -> None:
    clock = FakeClock()
    gate = PaidCallGate(min_interval=0, clock=clock, sleep=clock.sleep)
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def slow() -> str:
        first_started.set()
        await release_first.wait()
        return 'slow'

    async def fast() -> str:
        return 'fast'

    slow_task = asyncio.create_task(gate.run(slow))
    await first_started.wait()
    assert await asyncio.wait_for(gate.run(fast), timeout=1.0) == 'fast'
    release_first.set()
    assert await slow_task == 'slow'
