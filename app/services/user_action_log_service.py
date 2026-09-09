"""Лог действий пользователя для таймлайна активности в карточке юзера.

Поверхностей у пользователя три, и все три пишут в одну таблицу
``button_click_logs`` (без новых миграций), различаясь ``button_type``:

* кнопки бота — ``ButtonStatsMiddleware`` (тип ``None``/``builtin``/``callback``);
* кабинет — зависимость авторизации, тип ``cabinet``;
* Mini App — авторизация запроса в ``app/webapi/routes/miniapp.py``, тип ``miniapp``.

Третьей не было вовсе: у Mini App своя авторизация по ``init_data``, мимо
кабинетной зависимости. Человек, живущий в Mini App, выглядел в «Активности»
неактивным — с этого и началась жалоба.

Отличать действие от чтения по HTTP-методу в Mini App нельзя: ``init_data``
приходит телом, поэтому и чтения идут POST-ом. Поэтому список действий задан
явно, а тест-сторож требует, чтобы каждый маршрут был отнесён к действиям или к
чтениям — новый маршрут не проскочит молча.

Записи всех источников отдаёт GET /cabinet/admin/users/{id}/activity.
"""

from __future__ import annotations

import asyncio
import re
from contextvars import ContextVar, Token

import structlog

from app.config import settings
from app.database.database import AsyncSessionLocal


logger = structlog.get_logger(__name__)

CABINET_BUTTON_TYPE = 'cabinet'
MINIAPP_BUTTON_TYPE = 'miniapp'

_MUTATING_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})
# Технические/шумные пути: auth-обмены дергаются фоном, админские действия
# уже пишутся в admin_audit_log зависимостью require_permission.
_EXCLUDED_PREFIXES = ('/cabinet/admin', '/cabinet/auth/refresh')
_ID_SEGMENT_RE = re.compile(r'/\d+(?=/|$)')

# Действия человека в Mini App — то, что имеет смысл видеть в таймлайне.
MINIAPP_ACTION_PATHS = frozenset(
    {
        '/miniapp/devices/remove',
        '/miniapp/payments/create',
        '/miniapp/promo-codes/activate',
        '/miniapp/promo-offers/{id}/claim',
        '/miniapp/subscription/autopay',
        '/miniapp/subscription/daily/toggle-pause',
        '/miniapp/subscription/devices',
        '/miniapp/subscription/purchase',
        '/miniapp/subscription/renewal',
        '/miniapp/subscription/servers',
        '/miniapp/subscription/tariff/purchase',
        '/miniapp/subscription/tariff/switch',
        '/miniapp/subscription/traffic',
        '/miniapp/subscription/traffic-topup',
        '/miniapp/subscription/trial',
    }
)

# Чтения и предпросчёты: дёргаются при каждом открытии экрана, в таймлайне
# были бы шумом. Перечислены явно, чтобы сторож видел полный список маршрутов.
MINIAPP_READ_PATHS = frozenset(
    {
        '/miniapp/maintenance/status',
        '/miniapp/payments/methods',
        '/miniapp/payments/status',
        '/miniapp/subscription',
        '/miniapp/subscription/purchase/options',
        '/miniapp/subscription/purchase/preview',
        '/miniapp/subscription/renewal/options',
        '/miniapp/subscription/settings',
        '/miniapp/subscription/tariff/switch/preview',
        '/miniapp/subscription/tariffs',
    }
)

# Путь текущего запроса. Авторизация Mini App знает пользователя, но не путь:
# init_data приходит телом, поэтому единой зависимости с ``Request`` там нет.
_request_path: ContextVar[str | None] = ContextVar('user_action_request_path', default=None)

# Сильные ссылки на фоновые записи: без них цикл событий держит задачу только
# слабой ссылкой, и сборщик мусора вправе убить её на полпути (documented
# asyncio pitfall) — часть действий тихо терялась бы.
_pending_actions: set[asyncio.Task] = set()


def bind_request_path(path: str) -> Token:
    """Запомнить путь текущего запроса на время его обработки."""
    return _request_path.set(path)


def reset_request_path(token: Token) -> None:
    _request_path.reset(token)


def current_request_path() -> str | None:
    return _request_path.get()


def normalize_cabinet_path(path: str) -> str:
    """Сворачивает числовые сегменты пути в {id} для группировки однотипных действий."""
    return _ID_SEGMENT_RE.sub('/{id}', path)


def should_log_cabinet_action(method: str, path: str) -> bool:
    if not settings.USER_ACTION_LOG_ENABLED:
        return False
    if method.upper() not in _MUTATING_METHODS:
        return False
    return not path.startswith(_EXCLUDED_PREFIXES)


def should_log_miniapp_action(path: str) -> bool:
    if not settings.USER_ACTION_LOG_ENABLED:
        return False
    return normalize_cabinet_path(path) in MINIAPP_ACTION_PATHS


def schedule_cabinet_action_log(user_id: int, method: str, path: str) -> None:
    """Fire-and-forget запись действия юзера в кабинете — не задерживает запрос."""
    if not should_log_cabinet_action(method, path):
        return
    _spawn(
        user_id=user_id,
        button_id=f'{method.upper()} {normalize_cabinet_path(path)}'[:100],
        callback_data=path[:255],
        button_type=CABINET_BUTTON_TYPE,
    )


def schedule_miniapp_action_log(user_id: int, path: str | None = None) -> None:
    """Fire-and-forget запись действия юзера в Mini App."""
    resolved = path if path is not None else current_request_path()
    if not resolved or not should_log_miniapp_action(resolved):
        return
    _spawn(
        user_id=user_id,
        button_id=f'POST {normalize_cabinet_path(resolved)}'[:100],
        callback_data=resolved[:255],
        button_type=MINIAPP_BUTTON_TYPE,
    )


def _spawn(*, user_id: int, button_id: str, callback_data: str, button_type: str) -> None:
    try:
        task = asyncio.create_task(_write_action(user_id, button_id, callback_data, button_type))
    except RuntimeError:
        # Нет запущенного цикла событий — записывать некому и незачем.
        return
    _pending_actions.add(task)
    task.add_done_callback(_pending_actions.discard)


async def drain_pending_actions() -> None:
    """Дождаться фоновых записей (нужно тестам и корректному завершению)."""
    while _pending_actions:
        await asyncio.gather(*tuple(_pending_actions), return_exceptions=True)


async def _write_action(user_id: int, button_id: str, callback_data: str, button_type: str) -> None:
    try:
        async with AsyncSessionLocal() as db:
            from app.services.menu_layout.service import MenuLayoutService

            await MenuLayoutService.log_button_click(
                db,
                button_id=button_id,
                user_id=user_id,
                callback_data=callback_data,
                button_type=button_type,
                button_text=None,
            )
    except Exception as error:
        logger.debug('Не удалось записать действие юзера', error=str(error))


async def _write_cabinet_action(user_id: int, method: str, path: str) -> None:
    """Совместимость: прежняя точка входа для записи действия в кабинете."""
    await _write_action(
        user_id,
        f'{method.upper()} {normalize_cabinet_path(path)}'[:100],
        path[:255],
        CABINET_BUTTON_TYPE,
    )
