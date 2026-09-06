"""Ссылки конфигов подписки панели Remnawave для BSCHEKER.

У новых панелей ``GET /api/sub/{shortUuid}/info`` отдаёт пустой ``links`` (ручки уже нет в
OpenAPI), поэтому идём по трём источникам, пока не получим ссылки: защищённая
``/api/subscriptions/by-short-uuid``, устаревший ``/info``, публичный ``/api/sub/{shortUuid}``
с клиентским User-Agent (панель отдаёт настоящие ссылки только клиентам, браузеру — страницу).
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Awaitable, Callable
from typing import Any

import structlog


logger = structlog.get_logger(__name__)

# Панель различает клиентов по User-Agent; неизвестному отдаёт страницу или заглушки.
CLIENT_USER_AGENT = 'Happ/3.5.0'
_BASE64_RE = re.compile(r'^[A-Za-z0-9+/=_-]+$')


def _link_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if '://' in line]


def decode_subscription_body(text: str) -> list[str]:
    """Тело публичной подписки: ссылки построчно либо base64 от них; страница/мусор — пусто."""
    lines = _link_lines(text or '')
    if lines:
        return lines
    compact = ''.join((text or '').split())
    if not compact or not _BASE64_RE.match(compact):
        return []
    padded = compact.replace('-', '+').replace('_', '/') + '=' * (-len(compact) % 4)
    try:
        decoded = base64.b64decode(padded).decode('utf-8')
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return []
    return _link_lines(decoded)


async def _protected(api: Any, short_uuid: str) -> list[str]:
    return list(await api.get_subscription_links_by_short_uuid(short_uuid))


async def _legacy_info(api: Any, short_uuid: str) -> list[str]:
    return list((await api.get_subscription_info(short_uuid)).links or [])


async def _public(api: Any, short_uuid: str) -> list[str]:
    return decode_subscription_body(await api.get_subscription_by_short_uuid(short_uuid, user_agent=CLIENT_USER_AGENT))


_SOURCES: tuple[tuple[str, Callable[[Any, str], Awaitable[list[str]]]], ...] = (
    ('protected', _protected),
    ('info', _legacy_info),
    ('public', _public),
)


async def fetch_panel_links(api: Any, short_uuid: str) -> list[str]:
    """Первый непустой список ссылок из трёх источников; ошибки источника — в лог, не наружу."""
    for name, getter in _SOURCES:
        try:
            links = await getter(api, short_uuid)
        except Exception as exc:
            logger.info(
                'Ссылки подписки: источник не ответил', source=name, short_uuid=short_uuid, error=str(exc)[:200]
            )
            continue
        if links:
            logger.debug('Ссылки подписки получены', source=name, short_uuid=short_uuid, count=len(links))
            return [str(link) for link in links if link]
    logger.warning('Ссылки подписки: все источники пусты', short_uuid=short_uuid)
    return []
