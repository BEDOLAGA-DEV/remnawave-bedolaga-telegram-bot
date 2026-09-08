"""CABINET_EMAIL_AUTH_ENABLED должен закрывать email-вход целиком, а не только кнопку.

Баг из «Багов» (4.5.0, повторён на 4.6.0): флаг отдавался фронту, кнопка пропадала,
а /email/register/standalone и /email/login принимали прямые запросы — скрипт
регистрировал одноразовые почты и забирал триалы. Вторая половина бага: админский
переключатель в кабинете пишет строку в system_settings, а settings из окружения
о ней не знает, поэтому даже честная проверка `settings.is_cabinet_email_auth_enabled()`
разошлась бы с тем, что видит админ.

Инвариант: один резолвер флага (строка в БД важнее окружения) и один гейт в начале
каждого email/password-роута — до rate-limit, до обращения к Redis и к таблице users.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.database.models import SystemSetting
from tests.fixtures.sqlite_memory import memory_session


AUTH_FILE = Path(__file__).resolve().parents[2] / 'app' / 'cabinet' / 'routes' / 'auth.py'


class Reached(Exception):
    """Сентинел: обработчик прошёл гейт и дошёл до rate-limit."""


def _db_without_row():
    """Сессия, в которой строки флага нет: резолвер падает назад на settings."""
    return SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)))


# --------------------------------------------------------------------------- резолвер


@pytest.mark.asyncio
async def test_db_row_overrides_env(monkeypatch):
    from app.cabinet.auth import email_auth_gate as gate

    async with memory_session(monkeypatch, [SystemSetting.__table__]) as db:
        monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', True)
        db.add(SystemSetting(key=gate.EMAIL_AUTH_ENABLED_KEY, value='false'))
        await db.commit()
        assert await gate.is_email_auth_enabled(db) is False

        monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', False)
        row = (
            await db.execute(select(SystemSetting).where(SystemSetting.key == gate.EMAIL_AUTH_ENABLED_KEY))
        ).scalar_one()
        row.value = 'True'
        await db.commit()
        assert await gate.is_email_auth_enabled(db) is True


@pytest.mark.asyncio
async def test_env_applies_when_no_db_row(monkeypatch):
    from app.cabinet.auth import email_auth_gate as gate

    async with memory_session(monkeypatch, [SystemSetting.__table__]) as db:
        monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', False)
        assert await gate.is_email_auth_enabled(db) is False
        monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', True)
        assert await gate.is_email_auth_enabled(db) is True


@pytest.mark.asyncio
async def test_require_raises_403_with_machine_code(monkeypatch):
    from app.cabinet.auth import email_auth_gate as gate

    monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', False)
    with pytest.raises(HTTPException) as denied:
        await gate.require_email_auth_enabled(_db_without_row())
    assert denied.value.status_code == 403
    assert denied.value.detail['code'] == gate.EMAIL_AUTH_DISABLED_CODE

    monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', True)
    assert await gate.require_email_auth_enabled(_db_without_row()) is None


# --------------------------------------------------------------------------- роуты

GATED_HANDLERS = (
    'register_email',
    'verify_email_merge',
    'register_email_standalone',
    'verify_email',
    'resend_verification',
    'resend_verification_public',
    'login_email',
    'forgot_password',
    'reset_password',
)


def _call(handler_name: str, db):
    """Вызвать боевой обработчик с болванками: до гейта он ничего из них не трогает."""
    from app.cabinet.routes import auth

    handler = getattr(auth, handler_name)
    if handler_name == 'resend_verification':
        return handler(user=object(), db=db)
    kwargs = {'request': object(), 'raw_request': object(), 'db': db}
    if handler_name in {'register_email', 'verify_email_merge'}:
        kwargs['user'] = object()
    return handler(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize('handler_name', GATED_HANDLERS)
async def test_handler_refuses_before_touching_anything(monkeypatch, handler_name):
    from app.cabinet.auth import email_auth_gate as gate
    from app.cabinet.routes import auth

    monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', False)

    async def rate_limit_must_not_run(*_args, **_kwargs):
        raise AssertionError('гейт обязан стоять до rate-limit: выключенный вход не должен трогать Redis')

    monkeypatch.setattr(auth.RateLimitCache, 'is_ip_rate_limited', rate_limit_must_not_run)
    db = _db_without_row()

    with pytest.raises(HTTPException) as denied:
        await _call(handler_name, db)

    assert denied.value.status_code == 403
    assert denied.value.detail['code'] == gate.EMAIL_AUTH_DISABLED_CODE
    # единственное обращение к БД — чтение строки флага
    assert db.execute.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('handler_name', ('register_email_standalone', 'login_email'))
async def test_handler_proceeds_when_enabled(monkeypatch, handler_name):
    from app.cabinet.auth import email_auth_gate as gate
    from app.cabinet.routes import auth

    monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', True)

    async def reached(*_args, **_kwargs):
        raise Reached

    monkeypatch.setattr(auth.RateLimitCache, 'is_ip_rate_limited', reached)
    monkeypatch.setattr(auth, 'get_client_ip', lambda _request: '127.0.0.1')

    with pytest.raises(Reached):
        await _call(handler_name, _db_without_row())


# --------------------------------------------------------------------------- сторож


UNGATED_EMAIL_ROUTES = {
    # смена почты у уже вошедшего пользователя — это профиль, а не вход по email
    '/email/change',
    '/email/change/verify',
    '/email/change/cancel',
    '/email/change/status',
}


def _route_bodies(source: str) -> dict[str, str]:
    bodies: dict[str, str] = {}
    for match in re.finditer(r"@router\.(?:post|get)\('([^']+)'", source):
        start = match.start()
        nxt = source.find('@router.', match.end())
        bodies[match.group(1)] = source[start : nxt if nxt > 0 else len(source)]
    return bodies


def test_every_email_and_password_route_is_gated():
    """Новый /email/* или /password/* роут без гейта — красный тест, а не дыра в проде."""
    bodies = _route_bodies(AUTH_FILE.read_text(encoding='utf-8'))
    subject = {
        path: body
        for path, body in bodies.items()
        if (path.startswith('/email/') or path.startswith('/password/')) and path not in UNGATED_EMAIL_ROUTES
    }
    assert len(subject) == len(GATED_HANDLERS), sorted(subject)
    for path, body in subject.items():
        gate_at = body.find('await require_email_auth_enabled(db)')
        assert gate_at > 0, f'{path}: нет гейта email-входа'
        limiter_at = body.find('RateLimitCache')
        assert limiter_at < 0 or gate_at < limiter_at, f'{path}: гейт должен стоять до rate-limit'


# --------------------------------------------------------------------------- список провайдеров


@pytest.mark.asyncio
async def test_linked_providers_follow_the_same_switch(monkeypatch):
    """Список способов входа в профиле читает тот же переключатель, что UI и роуты."""
    from app.cabinet.auth import email_auth_gate as gate
    from app.cabinet.routes import account_linking

    async with memory_session(monkeypatch, [SystemSetting.__table__]) as db:
        monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', True)
        db.add(SystemSetting(key=gate.EMAIL_AUTH_ENABLED_KEY, value='false'))
        await db.commit()
        assert 'email' not in await account_linking._get_active_providers(db)

        row = (
            await db.execute(select(SystemSetting).where(SystemSetting.key == gate.EMAIL_AUTH_ENABLED_KEY))
        ).scalar_one()
        row.value = 'true'
        await db.commit()
        assert 'email' in await account_linking._get_active_providers(db)


# --------------------------------------------------------------------------- формат строки в БД


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('stored', 'expected'),
    [('true', True), ('True', True), ('1', True), ('yes', True), ('false', False), ('0', False), ('no', False)],
)
async def test_db_value_parsed_like_settings_editor(monkeypatch, stored, expected):
    """Строку пишут и админский переключатель ('true'), и общий редактор настроек, и
    инструмент переноса .env в БД (сырое '1'). Читать надо тем же парсером, что редактор."""
    from app.cabinet.auth import email_auth_gate as gate

    async with memory_session(monkeypatch, [SystemSetting.__table__]) as db:
        monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', not expected)
        db.add(SystemSetting(key=gate.EMAIL_AUTH_ENABLED_KEY, value=stored))
        await db.commit()
        assert await gate.is_email_auth_enabled(db) is expected


@pytest.mark.asyncio
async def test_garbage_db_value_falls_back_to_env(monkeypatch):
    from app.cabinet.auth import email_auth_gate as gate

    async with memory_session(monkeypatch, [SystemSetting.__table__]) as db:
        db.add(SystemSetting(key=gate.EMAIL_AUTH_ENABLED_KEY, value='maybe'))
        await db.commit()
        monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', False)
        assert await gate.is_email_auth_enabled(db) is False
        monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', True)
        assert await gate.is_email_auth_enabled(db) is True
