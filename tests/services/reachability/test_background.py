"""Фон сервиса: запуск идемпотентен, упавший обходчик перезапускается, остановка гасит его."""

from __future__ import annotations

import asyncio

import pytest

from app.services.reachability.gate import PaidCallGate
from app.services.reachability.jobs import JobRunner, RunnerConfig
from app.services.reachability.service import ReachabilityService
from tests.services.reachability.fakes import FakeAPI, FakeClock


pytestmark = pytest.mark.asyncio


def _service(session_factory) -> ReachabilityService:
    clock = FakeClock()
    runner = JobRunner(
        client_factory=FakeAPI,
        gate=PaidCallGate(min_interval=0, clock=clock, sleep=clock.sleep),
        session_factory=session_factory,
        cost_limit_kopeks=lambda: 0,
        config=RunnerConfig(sweep_interval=0.01),
        sleep=asyncio.sleep,
        clock=clock,
    )
    return ReachabilityService(session_factory=session_factory, runner=runner)


async def test_start_background_is_idempotent_and_stop_cancels(session_factory) -> None:
    service = _service(session_factory)
    service.start_background()
    first = service._background
    service.start_background()
    assert service._background is first
    await asyncio.sleep(0.05)
    assert not first.done()
    await service.stop_background()
    assert first.cancelled() and service._background is None


async def test_failed_background_is_restarted_on_next_start(session_factory) -> None:
    service = _service(session_factory)

    async def boom() -> None:
        raise RuntimeError('boom')

    service._background = asyncio.create_task(boom())
    await asyncio.sleep(0)
    dead = service._background
    assert dead.done() and isinstance(dead.exception(), RuntimeError)
    service.start_background()
    assert service._background is not dead and not service._background.done()
    await service.stop_background()


async def test_stop_without_start_is_noop(session_factory) -> None:
    service = _service(session_factory)
    await service.stop_background()
    assert service._background is None
