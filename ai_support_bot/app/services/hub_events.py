"""Дублирование событий поддержки в общий журнал slig-hub.

Журнал — вспомогательный канал: если адрес не задан, модуль ничего не делает, а любая ошибка
отправки остаётся в логе и никогда не влияет на ответ клиенту.
"""

import asyncio
from typing import Any

import aiohttp
import structlog

from ai_support_bot.app.core.config import settings


logger = structlog.get_logger(__name__)

SEND_TIMEOUT = 3.0
TITLE_LIMIT = 200
TEXT_LIMIT = 4000

# Ссылки на фоновые отправки: без них сборщик мусора может снять незавершённую задачу.
_tasks: set[asyncio.Task] = set()


def _build_payload(
    kind: str,
    title: str,
    text: str,
    severity: str,
    quote: str,
    data: dict[str, Any] | None,
    dedup_key: str | None,
    silent: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'kind': str(kind),
        'severity': str(severity or 'info'),
        'title': str(title or '')[:TITLE_LIMIT],
        'text': str(text or '')[:TEXT_LIMIT],
        'quote': str(quote or '')[:TEXT_LIMIT],
        'data': dict(data) if data else {},
        'silent': bool(silent),
    }
    if dedup_key:
        payload['dedup_key'] = str(dedup_key)
    return payload


async def _post(url: str, token: str, payload: dict[str, Any]) -> int:
    timeout = aiohttp.ClientTimeout(total=SEND_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers={'Authorization': f'Bearer {token}'}) as response:
            return response.status


async def _send(url: str, token: str, payload: dict[str, Any]) -> None:
    kind = payload.get('kind')
    try:
        status = await asyncio.wait_for(_post(url, token, payload), timeout=SEND_TIMEOUT)
    except Exception as error:
        logger.warning('Hub event not delivered', kind=kind, error_type=type(error).__name__)
        return
    if status >= 400:
        logger.warning('Hub event rejected', kind=kind, status=status)
    else:
        logger.debug('Hub event delivered', kind=kind, status=status)


def emit(
    kind: str,
    title: str,
    text: str = '',
    severity: str = 'info',
    quote: str = '',
    data: dict[str, Any] | None = None,
    dedup_key: str | None = None,
    silent: bool = False,
) -> None:
    """Отправить событие в журнал в фоне. Не ждёт сети и не поднимает исключений."""
    try:
        base_url = (settings.HUB_URL or '').strip().rstrip('/')
        if not base_url:
            return
        payload = _build_payload(kind, title, text, severity, quote, data, dedup_key, silent)
        task = asyncio.get_running_loop().create_task(
            _send(f'{base_url}/events', (settings.HUB_TOKEN or '').strip(), payload)
        )
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    except Exception as error:
        logger.warning('Hub event not scheduled', kind=kind, error_type=type(error).__name__)


async def drain() -> None:
    """Дождаться незавершённых отправок (для тестов и аккуратной остановки)."""
    if _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
