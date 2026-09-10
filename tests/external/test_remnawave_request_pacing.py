"""Свой темп запросов к панели.

Перед панелью часто стоит прокси с лимитом (шаблонный Caddyfile Remnawave:
``rate_limit`` 100 запросов в минуту с одного IP на ``/api/*``). Массовый проход
на 5 параллельных запросов упирался в него за секунды, дальше шли 429, общие
паузы и подписки «временно не приняты». Оператор, который не может поправить
прокси, задаёт ``REMNAWAVE_API_REQUESTS_PER_MINUTE`` — и бот держится под
порогом сам, скользящим окном в минуту на весь процесс. Ноль — без ограничения.
"""

from __future__ import annotations

import pytest

import app.external.remnawave_api as api_module
from app.config import settings
from app.external.remnawave_api import RemnaWaveAPI
from tests.external.test_remnawave_3_0_0 import _api_with_session, _FakeResponse


@pytest.fixture
def pace(monkeypatch):
    """Фальшивые часы: «сон» двигает их вперёд, реального ожидания нет."""
    clock = [1_000.0]
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(api_module.asyncio, 'sleep', fake_sleep)
    monkeypatch.setattr(RemnaWaveAPI, '_throttled_until', 0.0)
    monkeypatch.setattr(RemnaWaveAPI, '_pace_clock', lambda: clock[0])
    RemnaWaveAPI._request_times.clear()
    yield SimpleNamespaceLike(sleeps=sleeps, clock=clock)
    RemnaWaveAPI._request_times.clear()


class SimpleNamespaceLike:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _ok() -> _FakeResponse:
    return _FakeResponse(200, '{"response":{}}')


@pytest.mark.asyncio
async def test_no_pacing_by_default(pace, monkeypatch):
    monkeypatch.setattr(settings, 'REMNAWAVE_API_REQUESTS_PER_MINUTE', 0)
    api, session = _api_with_session(_ok(), _ok(), _ok())

    for _ in range(3):
        await api._make_request('GET', '/api/system/stats')

    assert len(session.calls) == 3
    assert pace.sleeps == []


@pytest.mark.asyncio
async def test_requests_beyond_the_minute_budget_wait_for_the_window(pace, monkeypatch):
    monkeypatch.setattr(settings, 'REMNAWAVE_API_REQUESTS_PER_MINUTE', 2)
    api, session = _api_with_session(_ok(), _ok(), _ok())

    for _ in range(3):
        await api._make_request('GET', '/api/system/stats')

    assert len(session.calls) == 3
    assert len(pace.sleeps) == 1 and 59.0 <= pace.sleeps[0] <= 60.0, pace.sleeps


@pytest.mark.asyncio
async def test_window_slides_and_frees_a_slot_after_a_minute(pace, monkeypatch):
    monkeypatch.setattr(settings, 'REMNAWAVE_API_REQUESTS_PER_MINUTE', 2)
    api, session = _api_with_session(_ok(), _ok(), _ok())

    await api._make_request('GET', '/api/system/stats')
    pace.clock[0] += 61  # первая минута прошла
    await api._make_request('GET', '/api/system/stats')
    await api._make_request('GET', '/api/system/stats')

    assert len(session.calls) == 3
    assert pace.sleeps == [], 'два запроса в новом окне укладываются в лимит без ожидания'
