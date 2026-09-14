"""Дроссели регистрации по email и повторной отправки письма подтверждения.

Одного минутного окна мало: скрипт ждёт минуту и продолжает — из отчёта про волну
одноразовых почт за триалами. Поверх него часовое и суточное окна; все три
настраиваются (0 выключает окно). Сработавшее окно отдаёт 429 и Retry-After на
свою длину. Redis недоступен — блокируем (fail_closed): регистрация security-critical.
"""

from __future__ import annotations

import hashlib

from fastapi import HTTPException, status

from app.config import settings
from app.utils.cache import RateLimitCache


MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR


def _windows() -> tuple[tuple[str, int, int], ...]:
    return (
        ('email_register', settings.CABINET_EMAIL_REGISTER_LIMIT_PER_MINUTE, MINUTE),
        ('email_register_hour', settings.CABINET_EMAIL_REGISTER_LIMIT_PER_HOUR, HOUR),
        ('email_register_day', settings.CABINET_EMAIL_REGISTER_LIMIT_PER_DAY, DAY),
    )


async def enforce_email_registration_throttle(client_ip: str) -> None:
    for action, limit, window in _windows():
        if limit <= 0:
            continue
        if await RateLimitCache.is_ip_rate_limited(client_ip, action, limit=limit, window=window, fail_closed=True):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail='Too many requests',
                headers={'Retry-After': str(window)},
            )


def _resend_ip_windows() -> tuple[tuple[str, int, int], ...]:
    return (
        ('email_resend', settings.CABINET_EMAIL_RESEND_LIMIT_PER_MINUTE, MINUTE),
        ('email_resend_hour', settings.CABINET_EMAIL_RESEND_LIMIT_PER_HOUR, HOUR),
    )


def _address_digest(email: str) -> str:
    """Ключ лимита не должен быть самим адресом: он оседает в Redis и в его логах."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:32]


async def enforce_verification_resend_throttle(client_ip: str, email: str) -> None:
    """Дроссель кнопки «Отправить письмо ещё раз» на экране «Проверьте почту».

    Экран открыт без входа в аккаунт, поэтому окна идут по IP — и отдельно по
    самому адресу: лимит по IP не спасает чужой ящик, если его заваливают
    письмами с разных адресов.

    Redis недоступен — блокируем (fail_closed): ручка неаутентифицированная и
    отправляет почту.
    """
    for action, limit, window in _resend_ip_windows():
        if limit <= 0:
            continue
        if await RateLimitCache.is_ip_rate_limited(client_ip, action, limit=limit, window=window, fail_closed=True):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail='Too many requests',
                headers={'Retry-After': str(window)},
            )

    per_address = settings.CABINET_EMAIL_RESEND_PER_ADDRESS_PER_HOUR
    if per_address > 0 and await RateLimitCache.is_subject_rate_limited(
        _address_digest(email), 'email_resend', limit=per_address, window=HOUR, fail_closed=True
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': str(HOUR)},
        )
