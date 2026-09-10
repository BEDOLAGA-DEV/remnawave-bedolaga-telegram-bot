"""Лог действий пользователя для таймлайна активности в карточке юзера.

Поверхностей у пользователя три, и все три пишут в одну таблицу
``button_click_logs`` (без новых миграций), различаясь ``button_type``:

* кнопки, команды и оплаты бота — ``ButtonStatsMiddleware``
  (тип ``None``/``builtin``/``callback``/``command``/``payment``);
* кабинет — зависимость авторизации пишет изменения (POST/PUT/PATCH/DELETE),
  а сам кабинет сообщает об открытии каждого экрана через
  ``POST /cabinet/activity/screen`` — тип ``cabinet``;
* Mini App — авторизация запроса в ``app/webapi/routes/miniapp.py``:
  действия и просмотры экранов — тип ``miniapp``.

Решение владельца: «Активность» существует, чтобы видеть каждый шаг — всё,
что человек делает и в боте, и в кабинете. Поэтому просмотры пишутся тоже.
Чтобы опрос статуса платежа или перерисовка экрана не давали десяток строк,
один и тот же экран одного человека в пределах минуты считается одной записью.

Отличать действие от чтения по HTTP-методу в Mini App нельзя: ``init_data``
приходит телом, поэтому и чтения идут POST-ом. Список действий задан явно,
чтения — тоже, а тест-сторож требует, чтобы каждый маршрут был отнесён к одному
из двух — новый маршрут не проскочит молча.

Записи всех источников отдаёт GET /cabinet/admin/users/{id}/activity.
"""

from __future__ import annotations

import asyncio
import re
import time
from contextvars import ContextVar, Token
from datetime import UTC, datetime, timedelta

import structlog

from app.config import settings
from app.database.database import AsyncSessionLocal


logger = structlog.get_logger(__name__)

CABINET_BUTTON_TYPE = 'cabinet'
MINIAPP_BUTTON_TYPE = 'miniapp'

# Открытие экрана: button_id = 'SCREEN <нормализованный путь>'.
SCREEN_PREFIX = 'SCREEN '
# Тот же экран того же человека внутри окна — одна запись (опрос статуса
# платежа, перерисовка, StrictMode в разработке).
SCREEN_DEDUP_SECONDS = 60.0
SCREEN_DEDUP_MAX_ENTRIES = 5000
# «Последнюю активность» двигаем не чаще раза в пять минут — иначе каждый
# запрос кабинета превращался бы в UPDATE users.
ACTIVITY_TOUCH_INTERVAL = timedelta(minutes=5)

_MUTATING_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})
# Технические/шумные пути: auth-обмены дергаются фоном, админские действия
# уже пишутся в admin_audit_log зависимостью require_permission, а отчёт об
# экране — сам источник записи, иначе каждый экран давал бы две строки.
_EXCLUDED_PREFIXES = ('/cabinet/admin', '/cabinet/auth/refresh', '/cabinet/activity')
_ID_SEGMENT_RE = re.compile(r'/\d+(?=/|$)')
# Экраны кабинета, где в пути живёт секрет (токен купона, подарка, слияния,
# входа по ссылке): всё после префикса маскируется, в журнал не попадает.
_SECRET_SCREEN_PREFIXES = (
    '/coupon',
    '/buy/gift',
    '/buy/success',
    '/merge',
    '/auto-login',
    '/verify-email',
    '/reset-password',
)
# Длинный «непроизносимый» сегмент — тоже токен, даже на незнакомом экране.
_OPAQUE_SEGMENT_RE = re.compile(r'^[A-Za-z0-9_-]{16,}$')

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

# Чтения и предпросчёты: дёргаются при каждом открытии экрана. Пишутся как
# просмотр экрана — одна запись на экран в минуту. Перечислены явно, чтобы
# сторож видел полный список маршрутов.
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

# (user_id, button_id) -> момент последней записи экрана.
_recent_screens: dict[tuple[int, str], float] = {}
_monotonic = time.monotonic


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


def normalize_screen_path(path: str) -> str:
    """Путь экрана без секретов: числа → {id}, токены → {token}, хвостовой слэш срезан."""
    clean = path.rstrip('/') or '/'
    for prefix in _SECRET_SCREEN_PREFIXES:
        if clean == prefix or clean.startswith(prefix + '/'):
            return f'{prefix}/{{token}}'
    segments = [
        '{id}' if segment.isdigit() else '{token}' if _OPAQUE_SEGMENT_RE.match(segment) else segment
        for segment in clean.split('/')
    ]
    return '/'.join(segments)


def mark_user_seen(user, *, now: datetime | None = None) -> bool:
    """Подвинуть ``last_activity``, если она старше интервала. Возвращает, изменилось ли.

    Двигают все три поверхности: по этой метке карточка показывает «последнюю
    активность», а сторож неактивных решает, кого удалять. Коммит — на вызывающем.
    """
    now = now or datetime.now(UTC)
    previous = getattr(user, 'last_activity', None)
    if previous is not None and previous.tzinfo is None:
        previous = previous.replace(tzinfo=UTC)
    if previous is not None and now - previous < ACTIVITY_TOUCH_INTERVAL:
        return False
    user.last_activity = now
    return True


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


def schedule_screen_view_log(user_id: int, path: str, *, surface: str = CABINET_BUTTON_TYPE) -> None:
    """Fire-and-forget запись открытия экрана; повтор в окне дедупликации — не пишется."""
    if not settings.USER_ACTION_LOG_ENABLED:
        return
    normalized = normalize_screen_path(path)
    button_id = f'{SCREEN_PREFIX}{normalized}'[:100]
    if _seen_recently(user_id, button_id):
        return
    _spawn(user_id=user_id, button_id=button_id, callback_data=normalized[:255], button_type=surface)


def schedule_miniapp_action_log(user_id: int, path: str | None = None) -> None:
    """Fire-and-forget запись шага юзера в Mini App: действие — как действие, чтение — как экран."""
    resolved = path if path is not None else current_request_path()
    if not resolved or not settings.USER_ACTION_LOG_ENABLED:
        return
    normalized = normalize_cabinet_path(resolved)
    if normalized in MINIAPP_ACTION_PATHS:
        _spawn(
            user_id=user_id,
            button_id=f'POST {normalized}'[:100],
            callback_data=resolved[:255],
            button_type=MINIAPP_BUTTON_TYPE,
        )
    elif normalized in MINIAPP_READ_PATHS:
        schedule_screen_view_log(user_id, resolved, surface=MINIAPP_BUTTON_TYPE)


def _seen_recently(user_id: int, button_id: str) -> bool:
    now = _monotonic()
    key = (user_id, button_id)
    last = _recent_screens.get(key)
    if last is not None and now - last < SCREEN_DEDUP_SECONDS:
        return True
    if len(_recent_screens) >= SCREEN_DEDUP_MAX_ENTRIES:
        _prune_recent_screens(now)
    _recent_screens[key] = now
    return False


def _prune_recent_screens(now: float) -> None:
    expired = [key for key, stamp in _recent_screens.items() if now - stamp >= SCREEN_DEDUP_SECONDS]
    for key in expired:
        del _recent_screens[key]
    # Всё живое и памяти всё равно много — забываем самое старое, точность
    # дедупликации тут дешевле, чем неограниченный словарь.
    if len(_recent_screens) >= SCREEN_DEDUP_MAX_ENTRIES:
        for key in sorted(_recent_screens, key=_recent_screens.__getitem__)[: SCREEN_DEDUP_MAX_ENTRIES // 2]:
            del _recent_screens[key]


def remember_task(task: object) -> None:
    """Держать фоновую задачу сильной ссылкой до завершения.

    Принимает что угодно: тесты middleware подменяют ``create_task`` заглушкой.
    """
    if not isinstance(task, asyncio.Task):
        return
    _pending_actions.add(task)
    task.add_done_callback(_pending_actions.discard)


def _spawn(*, user_id: int, button_id: str, callback_data: str | None, button_type: str) -> None:
    try:
        task = asyncio.create_task(_write_action(user_id, button_id, callback_data, button_type))
    except RuntimeError:
        # Нет запущенного цикла событий — записывать некому и незачем.
        return
    remember_task(task)


async def drain_pending_actions() -> None:
    """Дождаться фоновых записей (нужно тестам и корректному завершению)."""
    while _pending_actions:
        await asyncio.gather(*tuple(_pending_actions), return_exceptions=True)


async def _write_action(user_id: int, button_id: str, callback_data: str | None, button_type: str) -> None:
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
