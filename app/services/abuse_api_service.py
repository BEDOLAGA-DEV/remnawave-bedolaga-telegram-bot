"""Клиент внешнего антифрод-API.

Проверка злоупотреблений живёт снаружи: бот видит покупки и обращения, но не
видит подключений — с каких адресов, устройств и в каком порядке заходят под
одной подпиской. Эти данные есть у панели и у надстроек над ней, и они же
решают, злоупотребление это или совпадение.

Отсюда правило: бот ничего не вычисляет и не интерпретирует, а только
спрашивает и показывает. Если сервис не настроен, недоступен или ответил
медленно — считаем, что вопросов к человеку нет. Ошибка в эту сторону ничего
не ломает, а в обратную — обвиняет честного клиента и рушит экраны кабинета
из-за чужой недоступности.

Ожидаемый контракт (совместим с Remnawave Admin API v3):

* ``GET {base}/violations/summary?telegram_id=…`` → ``{"level": "clean|warned|limited", …}``
* ``GET {base}/violations?telegram_id=…&limit=…`` → список нарушений

Авторизация — заголовком ``X-API-Key``.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

# Долго ждать нельзя: ответ нужен внутри запроса кабинета, а сам вопрос
# необязательный — лучше показать экран без отметки, чем заставить человека
# смотреть на спиннер из-за чужого сервиса.
DEFAULT_TIMEOUT = 5


def is_configured() -> bool:
    return bool(
        getattr(settings, 'ABUSE_API_ENABLED', False)
        and getattr(settings, 'ABUSE_API_URL', None)
        and getattr(settings, 'ABUSE_API_KEY', None)
    )


def _base_url() -> str:
    return str(getattr(settings, 'ABUSE_API_URL', '') or '').rstrip('/')


def _timeout() -> int:
    try:
        return int(getattr(settings, 'ABUSE_API_TIMEOUT', DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


async def _get(path: str, params: dict[str, Any]) -> Any | None:
    if not is_configured():
        return None

    url = f'{_base_url()}{path}'
    headers = {'X-API-Key': str(getattr(settings, 'ABUSE_API_KEY', ''))}
    try:
        timeout = aiohttp.ClientTimeout(total=_timeout())
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params, headers=headers) as response:
                if response.status == 404:
                    return None
                if response.status >= 400:
                    logger.warning('Abuse API ответил ошибкой', path=path, status=response.status)
                    return None
                return await response.json()
    except Exception as error:
        logger.warning('Abuse API недоступен', path=path, error=str(error))
        return None


async def get_summary(telegram_id: int) -> dict | None:
    """Вердикт по клиенту: уровень доверия и последнее предупреждение.

    None — сервис не настроен либо не ответил; вызывающий обязан считать это
    отсутствием претензий, а не подозрением.
    """
    if not telegram_id:
        return None
    data = await _get('/violations/summary', {'telegram_id': telegram_id})
    return data if isinstance(data, dict) else None


async def get_violations(telegram_id: int, limit: int = 50) -> list[dict]:
    """История нарушений клиента — для операторов.

    Клиенту эти данные не показывают: перечень признаков на руках у нарушителя
    превращается в инструкцию по обходу.
    """
    if not telegram_id:
        return []
    data = await _get('/violations', {'telegram_id': telegram_id, 'limit': limit})
    return data if isinstance(data, list) else []


async def is_limited(telegram_id: int) -> bool:
    """Ограничен ли клиент по решению антифрода.

    Удобно там, где надо решить, выдавать ли триал или промо: молчание сервиса
    трактуется как «не ограничен».
    """
    summary = await get_summary(telegram_id)
    return bool(summary and summary.get('level') == 'limited')
