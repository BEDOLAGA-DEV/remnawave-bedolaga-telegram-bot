"""Внешний адрес ссылки, которую бот отдаёт наружу (файл, медиа), — с учётом обратного прокси.

uvicorn верит ``X-Forwarded-Proto`` только от 127.0.0.1, а прокси обычно живёт в соседнем контейнере: тогда
``request.url_for`` даёт ``http://<внутренний-адрес>``. Такую ссылку не скачает Telegram (``downloadFile`` —
только HTTPS), а браузер на https-кабинете заблокирует. Без заголовков прокси ссылка остаётся как есть.
"""

from __future__ import annotations

from typing import Any


_ALLOWED_SCHEMES = frozenset({'http', 'https'})


def _header(request: Any, name: str) -> str:
    """Первое значение заголовка (прокси могут дописывать цепочку через запятую); не строка — как нет."""
    value = getattr(request, 'headers', {}).get(name)
    return value.split(',')[0].strip() if isinstance(value, str) else ''


def public_url(request: Any, url: Any) -> str:
    """``url`` (обычно из ``request.url_for``) со схемой и хостом, которые видит клиент за прокси."""
    from starlette.datastructures import URL

    target = URL(str(url))
    own_scheme = getattr(getattr(request, 'url', None), 'scheme', None)
    proto = _header(request, 'x-forwarded-proto') or (own_scheme if isinstance(own_scheme, str) else target.scheme)
    if proto not in _ALLOWED_SCHEMES:
        proto = 'https'
    host = _header(request, 'x-forwarded-host') or _header(request, 'host') or target.netloc
    return str(target.replace(scheme=proto, netloc=host))
