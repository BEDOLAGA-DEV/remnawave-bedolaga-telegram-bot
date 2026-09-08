"""Медленные окна на регистрацию по email.

5 запросов в минуту с IP скрипт обходит ожиданием — из отчёта: «сессия просто ждёт
и продолжает». Поверх минутного окна добавлены часовое и суточное: все три настраиваются,
0 выключает окно. Сработавшее окно отдаёт 429 и Retry-After на его длину.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


def _limiter(tripped_action: str | None):
    calls: list[tuple[str, int, int]] = []

    async def is_ip_rate_limited(_ip: str, action: str, limit: int, window: int, *, fail_closed: bool = False):
        calls.append((action, limit, window))
        assert fail_closed is True, 'регистрация — security-critical: при недоступном Redis блокировать'
        return action == tripped_action

    return is_ip_rate_limited, calls


@pytest.mark.asyncio
async def test_windows_come_from_settings_and_are_checked_in_order(monkeypatch):
    from app.cabinet.auth import registration_throttle as throttle

    monkeypatch.setattr(throttle.settings, 'CABINET_EMAIL_REGISTER_LIMIT_PER_MINUTE', 5)
    monkeypatch.setattr(throttle.settings, 'CABINET_EMAIL_REGISTER_LIMIT_PER_HOUR', 10)
    monkeypatch.setattr(throttle.settings, 'CABINET_EMAIL_REGISTER_LIMIT_PER_DAY', 30)
    limiter, calls = _limiter(tripped_action=None)
    monkeypatch.setattr(throttle.RateLimitCache, 'is_ip_rate_limited', limiter)

    await throttle.enforce_email_registration_throttle('1.2.3.4')

    assert calls == [
        ('email_register', 5, 60),
        ('email_register_hour', 10, 3600),
        ('email_register_day', 30, 86400),
    ]


@pytest.mark.asyncio
async def test_tripped_slow_window_returns_429_with_its_retry_after(monkeypatch):
    from app.cabinet.auth import registration_throttle as throttle

    limiter, calls = _limiter(tripped_action='email_register_hour')
    monkeypatch.setattr(throttle.RateLimitCache, 'is_ip_rate_limited', limiter)

    with pytest.raises(HTTPException) as limited:
        await throttle.enforce_email_registration_throttle('1.2.3.4')

    assert limited.value.status_code == 429
    assert limited.value.headers['Retry-After'] == '3600'
    assert [c[0] for c in calls] == ['email_register', 'email_register_hour'], 'после срабатывания дальше не идём'


@pytest.mark.asyncio
async def test_zero_disables_a_window(monkeypatch):
    from app.cabinet.auth import registration_throttle as throttle

    monkeypatch.setattr(throttle.settings, 'CABINET_EMAIL_REGISTER_LIMIT_PER_HOUR', 0)
    monkeypatch.setattr(throttle.settings, 'CABINET_EMAIL_REGISTER_LIMIT_PER_DAY', 0)
    limiter, calls = _limiter(tripped_action=None)
    monkeypatch.setattr(throttle.RateLimitCache, 'is_ip_rate_limited', limiter)

    await throttle.enforce_email_registration_throttle('1.2.3.4')

    assert [c[0] for c in calls] == ['email_register']


@pytest.mark.asyncio
async def test_standalone_registration_uses_the_throttle(monkeypatch):
    """Боевой обработчик зовёт общий дроссель, а не свой минутный лимит."""
    from app.cabinet.auth import email_auth_gate as gate
    from app.cabinet.routes import auth

    monkeypatch.setattr(gate, 'get_setting_value', AsyncMock(return_value=None))
    monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', True)
    monkeypatch.setattr(auth, 'get_client_ip', lambda _request: '9.9.9.9')
    seen: list[str] = []

    async def throttle(ip: str) -> None:
        seen.append(ip)
        raise HTTPException(status_code=429, detail='Too many requests', headers={'Retry-After': '86400'})

    monkeypatch.setattr(auth, 'enforce_email_registration_throttle', throttle)

    with pytest.raises(HTTPException) as limited:
        await auth.register_email_standalone(request=object(), raw_request=object(), db=SimpleNamespace())

    assert limited.value.status_code == 429
    assert seen == ['9.9.9.9']
