"""Фоновый планировщик пересчёта промогрупп и сводка админам.

Один проход за раз: правка трёх групп подряд не должна запускать три
параллельных прохода по тысячам людей. Запрос во время прохода — ещё один
проход после текущего, чтобы не потерять изменения, сделанные пока шёл первый.
Сводка админам одна на проход и только когда есть что сказать.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services.promo_group_recalculation import (
    PromoGroupRecalculation,
    RecalculationResult,
    build_summary_text,
    notify_admins_about_recalculation,
)


def _result(reason: str, *, changed: int = 0, **extra) -> RecalculationResult:
    return RecalculationResult(reason=reason, checked=10, changed=changed, **extra)


class Announcer:
    def __init__(self) -> None:
        self.results: list[RecalculationResult] = []

    async def __call__(self, result: RecalculationResult) -> None:
        self.results.append(result)


class GatedRunner:
    """Проход, который держится, пока тест не откроет ворота."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.gate = asyncio.Event()

    async def __call__(self, reason: str) -> RecalculationResult:
        self.calls.append(reason)
        await self.gate.wait()
        return _result(reason, changed=2)


@pytest.mark.asyncio
async def test_schedule_runs_one_pass_and_announces_it():
    runner, announcer = GatedRunner(), Announcer()
    runner.gate.set()
    recalculation = PromoGroupRecalculation(runner=runner, announcer=announcer)

    assert recalculation.schedule('создана группа «Властелин»') is True
    await recalculation.wait()

    assert runner.calls == ['создана группа «Властелин»']
    assert [result.changed for result in announcer.results] == [2]
    snapshot = recalculation.snapshot()
    assert (snapshot['running'], snapshot['queued']) == (False, False)
    assert snapshot['last']['reason'] == 'создана группа «Властелин»'
    assert snapshot['last']['changed'] == 2


@pytest.mark.asyncio
async def test_requests_during_a_pass_collapse_into_one_more_pass():
    runner, announcer = GatedRunner(), Announcer()
    recalculation = PromoGroupRecalculation(runner=runner, announcer=announcer)

    assert recalculation.schedule('первая') is True
    await asyncio.sleep(0)
    assert recalculation.schedule('вторая') is False
    assert recalculation.schedule('третья') is False
    snapshot = recalculation.snapshot()
    assert (snapshot['running'], snapshot['queued'], snapshot['reason']) == (True, True, 'первая')

    runner.gate.set()
    await recalculation.wait()

    assert runner.calls == ['первая', 'третья'], 'между проходами — один повтор, с последней причиной'
    assert len(announcer.results) == 2
    snapshot = recalculation.snapshot()
    assert (snapshot['running'], snapshot['queued']) == (False, False)
    assert snapshot['last']['reason'] == 'третья'


@pytest.mark.asyncio
async def test_runner_failure_is_recorded_and_still_announced():
    async def broken(reason: str) -> RecalculationResult:
        raise RuntimeError('база недоступна')

    announcer = Announcer()
    recalculation = PromoGroupRecalculation(runner=broken, announcer=announcer)

    recalculation.schedule('удалена группа')
    await recalculation.wait()

    assert recalculation.last_result is not None
    assert recalculation.last_result.error == 'база недоступна'
    assert [result.error for result in announcer.results] == ['база недоступна']
    assert recalculation.is_running is False


def test_schedule_without_event_loop_is_refused_quietly():
    recalculation = PromoGroupRecalculation(runner=AsyncMock(), announcer=AsyncMock())

    assert recalculation.schedule('без цикла') is False
    assert recalculation.snapshot()['running'] is False


@pytest.fixture
def admin_chat(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'BOT_TOKEN', '123:token', raising=False)
    sent: list[tuple[str, object]] = []
    bots: list[object] = []

    def fake_create_bot(token: str):
        bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
        bots.append(bot)
        return bot

    class FakeService:
        def __init__(self, bot):
            self.bot = bot

        async def send_admin_notification(self, text, reply_markup=None, *, category=None):
            sent.append((text, category))
            return True

    monkeypatch.setattr('app.bot_factory.create_bot', fake_create_bot)
    monkeypatch.setattr('app.services.admin_notification_service.AdminNotificationService', FakeService)
    return SimpleNamespace(sent=sent, bots=bots)


@pytest.mark.asyncio
async def test_summary_is_silent_when_nobody_changed(admin_chat):
    await notify_admins_about_recalculation(_result('изменён порог', changed=0))

    assert admin_chat.sent == []
    assert admin_chat.bots == [], 'бот даже не создаётся'


@pytest.mark.asyncio
async def test_summary_is_sent_once_when_someone_changed(admin_chat):
    await notify_admins_about_recalculation(_result('создана группа «<Властелин>»', changed=2))

    assert len(admin_chat.sent) == 1
    text, category = admin_chat.sent[0]
    assert 'Переназначено: 2' in text
    assert 'Проверено: 10' in text
    assert '&lt;Властелин&gt;' in text, 'причина экранируется — это HTML'
    assert str(category) == 'promo'
    admin_chat.bots[0].session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_summary_is_sent_when_the_pass_failed(admin_chat):
    await notify_admins_about_recalculation(_result('удалена группа', error='база недоступна'))

    assert len(admin_chat.sent) == 1
    assert 'база недоступна' in admin_chat.sent[0][0]


def test_summary_text_lists_failures_only_when_there_are_any():
    quiet = build_summary_text(_result('вручную', changed=1))
    noisy = build_summary_text(_result('вручную', changed=1, failed=3))

    assert 'Не удалось' not in quiet
    assert 'Не удалось: 3' in noisy
