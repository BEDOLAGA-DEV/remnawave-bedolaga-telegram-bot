"""Сквозная проверка гейта email-входа через настоящее FastAPI-приложение.

Юнит-тесты зовут обработчики напрямую с болванками; здесь запрос проходит роутер,
зависимости и сессию к настоящей (in-memory) БД — как curl из багрепорта.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.auth import email_auth_gate as gate
from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.cabinet.routes.auth import router as auth_router
from app.database.models import SystemSetting, User
from tests.fixtures.sqlite_memory import memory_session


TABLES = (SystemSetting.__table__, User.__table__)

REGISTER_BODY = {
    'email': 'test@example.org',
    'password': 'Str0ng-Passw0rd!',
    'first_name': 'Test',
    'accepted_legal_documents': ['public_offer', 'privacy_policy'],
}
LOGIN_BODY = {'email': 'test@example.org', 'password': 'Str0ng-Passw0rd!'}


def _app(db: AsyncSession) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router, prefix='/cabinet')

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_cabinet_db] = _db
    return app


async def _set_flag(db: AsyncSession, value: str) -> None:
    db.add(SystemSetting(key=gate.EMAIL_AUTH_ENABLED_KEY, value=value))
    await db.commit()


async def _users_count(db: AsyncSession) -> int:
    return (await db.execute(select(func.count()).select_from(User))).scalar_one()


@pytest.mark.asyncio
async def test_disabled_in_admin_refuses_register_and_login_over_http(monkeypatch):
    """Админ выключил email-вход в кабинете (строка в БД), окружение говорит «включён»:
    оба curl из отчёта получают 403 с кодом, пользователь не создаётся."""
    async with memory_session(monkeypatch, TABLES) as db:
        monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', True)
        await _set_flag(db, 'false')

        async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url='http://cabinet') as client:
            register = await client.post('/cabinet/auth/email/register/standalone', json=REGISTER_BODY)
            login = await client.post('/cabinet/auth/email/login', json=LOGIN_BODY)

        assert register.status_code == 403, register.text
        assert register.json()['detail']['code'] == gate.EMAIL_AUTH_DISABLED_CODE
        assert login.status_code == 403, login.text
        assert login.json()['detail']['code'] == gate.EMAIL_AUTH_DISABLED_CODE
        assert await _users_count(db) == 0


@pytest.mark.asyncio
async def test_enabled_in_admin_lets_request_through_the_gate(monkeypatch):
    """Обратная сторона: строка 'true' в БД при выключенном окружении — запрос проходит гейт
    и упирается уже в обычную проверку пароля (401), а не в 403."""
    from app.cabinet.routes import auth

    async def not_limited(*_args, **_kwargs):
        return False

    async with memory_session(monkeypatch, TABLES) as db:
        monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', False)
        monkeypatch.setattr(auth.RateLimitCache, 'is_ip_rate_limited', not_limited)
        await _set_flag(db, 'true')

        async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url='http://cabinet') as client:
            login = await client.post('/cabinet/auth/email/login', json=LOGIN_BODY)

        assert login.status_code == 401, login.text
        assert login.json()['detail'] == 'Invalid email or password'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('path', 'body'),
    [
        ('/email/register/standalone', REGISTER_BODY),
        ('/email/register', LOGIN_BODY),
        ('/email/login', LOGIN_BODY),
        ('/email/verify', {'token': 'masked-1'}),
        ('/email/merge/verify', {'code': '123456'}),
        ('/email/resend', None),
        ('/email/register/resend', {'email': LOGIN_BODY['email']}),
        ('/password/forgot', {'email': LOGIN_BODY['email']}),
        ('/password/reset', {'token': 'masked-2', 'password': LOGIN_BODY['password']}),
    ],
)
async def test_db_disabled_blocks_all_email_routes_without_mutations_or_tokens(monkeypatch, path, body):
    """Прямой HTTP-вызов, включая OTP, не обходит DB-запрет при включённом env."""
    from app.cabinet.routes import auth

    async with memory_session(monkeypatch, TABLES) as db:
        monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', True)
        await _set_flag(db, 'false')
        user = User(
            email=LOGIN_BODY['email'],
            password_hash=auth.hash_password(LOGIN_BODY['password']),
            email_verified=False,
            email_verification_token='masked-1',
            email_verification_expires=datetime.now(UTC) + timedelta(hours=1),
            password_reset_token='masked-2',
            password_reset_expires=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add(user)
        await db.commit()
        before = (await db.execute(select(User.__table__))).mappings().one()

        # Даже уже существующие подтверждения и пароль не должны выдавать сессию.
        token_factories = []
        for name in (
            'create_access_token',
            'create_refresh_token',
            'create_merge_token',
            'generate_verification_token',
            'generate_password_reset_token',
            'generate_email_change_code',
        ):
            factory = Mock(side_effect=AssertionError('Disabled email auth must not issue tokens'))
            monkeypatch.setattr(auth, name, factory)
            token_factories.append(factory)

        app = _app(db)
        app.dependency_overrides[get_current_cabinet_user] = lambda: user
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://cabinet') as client:
            response = await client.post(f'/cabinet/auth{path}', json=body)

        assert response.status_code == 403, response.text
        assert response.json() == {
            'detail': {'code': gate.EMAIL_AUTH_DISABLED_CODE, 'message': 'Email authentication is disabled'}
        }
        assert not db.new and not db.dirty and not db.deleted
        assert await _users_count(db) == 1
        assert (await db.execute(select(User.__table__))).mappings().one() == before
        for factory in token_factories:
            factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [True, False])
async def test_public_email_config_matches_db_override_over_http(monkeypatch, enabled):
    """TG-079: the login page sees the effective toggle, not SMTP/env alone."""
    from app.cabinet.routes.branding import router as branding_router

    async with memory_session(monkeypatch, TABLES) as db:
        monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', not enabled)
        await _set_flag(db, str(enabled).lower())
        app = _app(db)
        app.include_router(branding_router, prefix='/cabinet')
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://cabinet') as client:
            response = await client.get('/cabinet/branding/email-auth')
        assert response.status_code == 200
        assert response.json()['enabled'] is enabled
