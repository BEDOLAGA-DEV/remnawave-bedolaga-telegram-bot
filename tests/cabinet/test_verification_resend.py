"""Кнопка «Отправить письмо ещё раз» на экране «Проверьте почту».

Экран показывается сразу после регистрации, войти ещё нельзя — значит ручка
работает без токена. Отсюда два свойства, которые тут и держатся: ответ не
должен отличаться для существующего и несуществующего адреса (иначе ручкой
проверяют чужие ящики), и отправку надо дросселировать не только по IP, но и по
самому адресу — иначе ящик заваливают письмами от нашего имени с разных адресов.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.auth import email_auth_gate as gate
from app.cabinet.dependencies import get_cabinet_db
from app.cabinet.routes.auth import router as auth_router
from app.database.models import SystemSetting, User
from tests.fixtures.sqlite_memory import memory_session


TABLES = (SystemSetting.__table__, User.__table__)

NEUTRAL = 'If the email is awaiting confirmation, the verification link has been sent'
RESEND_URL = '/cabinet/auth/email/register/resend'


# ==================== дроссель ====================


def _ip_limiter(tripped_action: str | None):
    calls: list[tuple[str, int, int]] = []

    async def is_ip_rate_limited(_ip: str, action: str, limit: int, window: int, *, fail_closed: bool = False):
        calls.append((action, limit, window))
        assert fail_closed is True, 'ручка неаутентифицированная и шлёт почту: без Redis блокировать'
        return action == tripped_action

    return is_ip_rate_limited, calls


def _subject_limiter(tripped: bool):
    calls: list[tuple[str, str, int, int]] = []

    async def is_subject_rate_limited(subject: str, action: str, limit: int, window: int, *, fail_closed: bool = False):
        calls.append((subject, action, limit, window))
        assert fail_closed is True
        return tripped

    return is_subject_rate_limited, calls


@pytest.mark.asyncio
async def test_windows_come_from_settings(monkeypatch):
    from app.cabinet.auth import registration_throttle as throttle

    monkeypatch.setattr(throttle.settings, 'CABINET_EMAIL_RESEND_LIMIT_PER_MINUTE', 2)
    monkeypatch.setattr(throttle.settings, 'CABINET_EMAIL_RESEND_LIMIT_PER_HOUR', 10)
    monkeypatch.setattr(throttle.settings, 'CABINET_EMAIL_RESEND_PER_ADDRESS_PER_HOUR', 5)
    ip_limiter, ip_calls = _ip_limiter(tripped_action=None)
    subject_limiter, subject_calls = _subject_limiter(tripped=False)
    monkeypatch.setattr(throttle.RateLimitCache, 'is_ip_rate_limited', ip_limiter)
    monkeypatch.setattr(throttle.RateLimitCache, 'is_subject_rate_limited', subject_limiter)

    await throttle.enforce_verification_resend_throttle('1.2.3.4', 'Ivan@Example.ORG')

    assert ip_calls == [('email_resend', 2, 60), ('email_resend_hour', 10, 3600)]
    assert [(c[1], c[2], c[3]) for c in subject_calls] == [('email_resend', 5, 3600)]


@pytest.mark.asyncio
async def test_address_key_is_a_digest_and_ignores_case(monkeypatch):
    """Ключ лимита оседает в Redis и в его логах — сам адрес туда попадать не должен."""
    from app.cabinet.auth import registration_throttle as throttle

    ip_limiter, _ = _ip_limiter(tripped_action=None)
    subject_limiter, subject_calls = _subject_limiter(tripped=False)
    monkeypatch.setattr(throttle.RateLimitCache, 'is_ip_rate_limited', ip_limiter)
    monkeypatch.setattr(throttle.RateLimitCache, 'is_subject_rate_limited', subject_limiter)

    await throttle.enforce_verification_resend_throttle('1.2.3.4', ' Ivan@Example.ORG ')
    await throttle.enforce_verification_resend_throttle('5.6.7.8', 'ivan@example.org')

    first, second = subject_calls[0][0], subject_calls[1][0]
    assert first == second, 'один ящик — один счётчик, независимо от регистра и пробелов'
    assert 'ivan' not in first.lower() and 'example' not in first.lower()


@pytest.mark.asyncio
async def test_one_inbox_is_protected_across_addresses(monkeypatch):
    """Лимит по IP не спасает чужой ящик: окно по адресу отдаёт 429 само по себе."""
    from app.cabinet.auth import registration_throttle as throttle

    ip_limiter, _ = _ip_limiter(tripped_action=None)
    subject_limiter, _ = _subject_limiter(tripped=True)
    monkeypatch.setattr(throttle.RateLimitCache, 'is_ip_rate_limited', ip_limiter)
    monkeypatch.setattr(throttle.RateLimitCache, 'is_subject_rate_limited', subject_limiter)

    with pytest.raises(HTTPException) as limited:
        await throttle.enforce_verification_resend_throttle('1.2.3.4', 'victim@example.org')

    assert limited.value.status_code == 429
    assert limited.value.headers['Retry-After'] == '3600'


@pytest.mark.asyncio
async def test_zero_disables_the_address_window(monkeypatch):
    from app.cabinet.auth import registration_throttle as throttle

    monkeypatch.setattr(throttle.settings, 'CABINET_EMAIL_RESEND_PER_ADDRESS_PER_HOUR', 0)
    ip_limiter, _ = _ip_limiter(tripped_action=None)
    subject_limiter, subject_calls = _subject_limiter(tripped=True)
    monkeypatch.setattr(throttle.RateLimitCache, 'is_ip_rate_limited', ip_limiter)
    monkeypatch.setattr(throttle.RateLimitCache, 'is_subject_rate_limited', subject_limiter)

    await throttle.enforce_verification_resend_throttle('1.2.3.4', 'ivan@example.org')

    assert subject_calls == []


# ==================== ручка целиком ====================


def _app(db: AsyncSession) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router, prefix='/cabinet')

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_cabinet_db] = _db
    return app


def _arm(monkeypatch, *, throttled: bool = False) -> MagicMock:
    """Верификация включена, почта настроена, дроссель пропускает. Отдаёт шпиона отправки."""
    from app.cabinet.routes import auth

    monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', True)
    monkeypatch.setattr(auth.settings, 'CABINET_EMAIL_VERIFICATION_ENABLED', True)
    monkeypatch.setattr(auth.settings, 'CABINET_URL', 'https://cabinet.example.org')

    async def throttle(_ip: str, _email: str) -> None:
        if throttled:
            raise HTTPException(status_code=429, detail='Too many requests', headers={'Retry-After': '60'})

    monkeypatch.setattr(auth, 'enforce_verification_resend_throttle', throttle)

    async def no_override(*_args, **_kwargs):
        return None

    monkeypatch.setattr(auth, 'get_rendered_override', no_override)

    sender = MagicMock(return_value=True)
    monkeypatch.setattr(auth.email_service, 'is_configured', MagicMock(return_value=True))
    monkeypatch.setattr(auth.email_service, 'send_verification_email', sender)
    return sender


async def _add_user(db: AsyncSession, *, email: str, verified: bool, token: str | None) -> User:
    user = User(
        telegram_id=None,
        email=email,
        email_verified=verified,
        email_verification_token=token,
        first_name='Иван',
        language='ru',
        auth_type='email',
    )
    db.add(user)
    await db.commit()
    return user


async def _token_of(db: AsyncSession, email: str) -> str | None:
    user = (await db.execute(select(User).where(User.email == email))).scalar_one()
    await db.refresh(user)
    return user.email_verification_token


@pytest.mark.asyncio
async def test_pending_address_gets_a_fresh_link(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sender = _arm(monkeypatch)
        await _add_user(db, email='ivan@example.org', verified=False, token='old-token')

        async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url='http://cabinet') as client:
            response = await client.post(RESEND_URL, json={'email': 'IVAN@example.org'})

        assert response.status_code == 200, response.text
        assert response.json()['message'] == NEUTRAL
        assert sender.call_count == 1
        assert sender.call_args.kwargs['to_email'] == 'ivan@example.org'
        # Старая ссылка обязана перестать работать: иначе «отправить ещё раз»
        # оставляет в живых письмо, которое пользователь считает устаревшим.
        assert await _token_of(db, 'ivan@example.org') not in (None, 'old-token')


@pytest.mark.asyncio
async def test_unknown_address_answers_the_same_and_sends_nothing(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sender = _arm(monkeypatch)

        async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url='http://cabinet') as client:
            response = await client.post(RESEND_URL, json={'email': 'stranger@example.org'})

        assert response.status_code == 200, response.text
        assert response.json()['message'] == NEUTRAL
        assert sender.call_count == 0


@pytest.mark.asyncio
async def test_verified_address_answers_the_same_and_keeps_its_token(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sender = _arm(monkeypatch)
        await _add_user(db, email='done@example.org', verified=True, token=None)

        async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url='http://cabinet') as client:
            response = await client.post(RESEND_URL, json={'email': 'done@example.org'})

        assert response.status_code == 200, response.text
        assert response.json()['message'] == NEUTRAL
        assert sender.call_count == 0
        assert await _token_of(db, 'done@example.org') is None


@pytest.mark.asyncio
async def test_throttle_stops_the_send(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sender = _arm(monkeypatch, throttled=True)
        await _add_user(db, email='ivan@example.org', verified=False, token='old-token')

        async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url='http://cabinet') as client:
            response = await client.post(RESEND_URL, json={'email': 'ivan@example.org'})

        assert response.status_code == 429, response.text
        assert sender.call_count == 0
        assert await _token_of(db, 'ivan@example.org') == 'old-token'
