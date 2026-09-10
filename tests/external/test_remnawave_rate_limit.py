"""429 от панели — это троттлинг, а не ошибка приложения.

Массовый проход «в панель» шёл пятью параллельными PATCH, панель отвечала 429, клиент
повторял через 1–4 с и падал с ошибкой: половина подписок не доезжала, а каждый
провал улетал отчётом в админ-чат. Теперь 429 ставит ОБЩУЮ паузу на все запросы
процесса (Retry-After или растущая задержка), повторов больше, а исчерпание
повторов — транзиентная ошибка (warning), которую форвардер админ-чата пропускает.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

import app.external.remnawave_api as api_module
from app.external.remnawave_api import RemnaWaveAPI, RemnaWaveTransientError
from tests.external.test_remnawave_3_0_0 import _api_with_session, _FakeResponse


@pytest.fixture(autouse=True)
def _no_real_sleep_and_clean_throttle(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(api_module.asyncio, 'sleep', fake_sleep)
    monkeypatch.setattr(RemnaWaveAPI, '_throttled_until', 0.0)
    yield sleeps
    monkeypatch.setattr(RemnaWaveAPI, '_throttled_until', 0.0)


def _rate_limited(retry_after: str | None = None) -> _FakeResponse:
    response = _FakeResponse(429, '{"message":"Too Many Requests"}')
    if retry_after is not None:
        response.headers['Retry-After'] = retry_after
    return response


async def test_429_retries_with_growing_delays_until_success(_no_real_sleep_and_clean_throttle):
    sleeps = _no_real_sleep_and_clean_throttle
    api, session = _api_with_session(_rate_limited(), _rate_limited(), _FakeResponse(200, '{"response":{"ok":1}}'))

    result = await api._make_request('GET', '/api/system/stats')

    assert result == {'response': {'ok': 1}}
    assert len(session.calls) == 3
    assert len(sleeps) == 2 and sleeps[0] >= 1.9 and sleeps[1] >= sleeps[0] * 1.5


async def test_429_honours_retry_after_header(_no_real_sleep_and_clean_throttle):
    sleeps = _no_real_sleep_and_clean_throttle
    api, _session = _api_with_session(_rate_limited('7'), _FakeResponse(200, '{"response":{}}'))

    await api._make_request('GET', '/api/system/stats')

    assert len(sleeps) == 1 and sleeps[0] == pytest.approx(7, abs=0.05)


async def test_429_sets_shared_throttle_for_other_requests(_no_real_sleep_and_clean_throttle):
    api, _session = _api_with_session(_rate_limited('5'), _FakeResponse(200, '{"response":{}}'))

    await api._make_request('GET', '/api/system/stats')

    assert RemnaWaveAPI._throttled_until >= time.monotonic() + 4


async def test_request_waits_for_shared_throttle_before_sending(_no_real_sleep_and_clean_throttle, monkeypatch):
    sleeps = _no_real_sleep_and_clean_throttle
    monkeypatch.setattr(RemnaWaveAPI, '_throttled_until', time.monotonic() + 3)
    api, session = _api_with_session(_FakeResponse(200, '{"response":{}}'))

    await api._make_request('GET', '/api/system/stats')

    assert len(session.calls) == 1
    assert sleeps and 2.5 <= sleeps[0] <= 3


async def test_429_after_all_retries_is_transient_and_not_logged_as_error(monkeypatch):
    fake_logger = MagicMock()
    monkeypatch.setattr(api_module, 'logger', fake_logger)
    api, session = _api_with_session(_rate_limited())

    with pytest.raises(RemnaWaveTransientError) as raised:
        await api._make_request('PATCH', '/api/users', {'id': 1})

    assert raised.value.status_code == 429
    assert len(session.calls) == api_module.RATE_LIMIT_MAX_RETRIES + 1
    fake_logger.error.assert_not_called()
    assert fake_logger.warning.called
