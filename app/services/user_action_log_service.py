"""Лог действий пользователя для таймлайна активности в карточке юзера.

Нажатия callback-кнопок в боте пишет ButtonStatsMiddleware; этот модуль
дописывает вторую половину — мутационные запросы юзера в кабинете — в ту же
таблицу button_click_logs (button_type='cabinet'), без новых миграций.
Записи обоих источников отдаёт GET /cabinet/admin/users/{id}/activity.
"""

import asyncio
import re

import structlog

from app.config import settings
from app.database.database import AsyncSessionLocal


logger = structlog.get_logger(__name__)

CABINET_BUTTON_TYPE = 'cabinet'

_MUTATING_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})
# Технические/шумные пути: auth-обмены дергаются фоном, админские действия
# уже пишутся в admin_audit_log зависимостью require_permission.
_EXCLUDED_PREFIXES = ('/cabinet/admin', '/cabinet/auth/refresh')
_ID_SEGMENT_RE = re.compile(r'/\d+(?=/|$)')


def normalize_cabinet_path(path: str) -> str:
    """Сворачивает числовые сегменты пути в {id} для группировки однотипных действий."""
    return _ID_SEGMENT_RE.sub('/{id}', path)


def should_log_cabinet_action(method: str, path: str) -> bool:
    if not settings.USER_ACTION_LOG_ENABLED:
        return False
    if method.upper() not in _MUTATING_METHODS:
        return False
    return not path.startswith(_EXCLUDED_PREFIXES)


def schedule_cabinet_action_log(user_id: int, method: str, path: str) -> None:
    """Fire-and-forget запись действия юзера в кабинете — не задерживает запрос."""
    if not should_log_cabinet_action(method, path):
        return
    asyncio.create_task(_write_cabinet_action(user_id, method.upper(), path))


async def _write_cabinet_action(user_id: int, method: str, path: str) -> None:
    try:
        async with AsyncSessionLocal() as db:
            from app.services.menu_layout.service import MenuLayoutService

            await MenuLayoutService.log_button_click(
                db,
                button_id=f'{method} {normalize_cabinet_path(path)}'[:100],
                user_id=user_id,
                callback_data=path[:255],
                button_type=CABINET_BUTTON_TYPE,
                button_text=None,
            )
    except Exception as error:
        logger.debug('Не удалось записать действие юзера в кабинете', error=str(error))
