"""Кто сейчас подключён к VPN — по панели, а не по кнопкам в боте.

«Онлайн» в админке кабинета раньше значил «нажимал что-то в боте за 5 минут»
(``User.last_activity``), а карточка пользователя — отметку панели
``userTraffic.onlineAt``. Список и карточка спорили: человек, сидящий в VPN, в
списке был «2 дня назад», а открывший бота без подключения — «онлайн».

Владельцу VPN «онлайн» — это подключение. Панель ставит ``onlineAt`` по отчётам нод
и сама красит пользователя зелёным, пока с отметки прошло не больше минуты; здесь
то же окно. Фильтр по ``onlineAt`` у ``GET /api/users`` — только точное равенство,
выбрать им «свежее минуты» нельзя. Зато по нему сортирует таблица самой панели:
берём список по убыванию отметки и листаем, пока она свежее окна.

Хранится и кэшируется **снимок отметок**, а не готовый ответ «эти онлайн». Кто
онлайн — считается в момент запроса: иначе ответ панели, взятый секунду назад,
до конца жизни кэша называл бы онлайн тех, кого сама панель уже погасила. Ошибаться
такой пересчёт может только в безопасную сторону — не заметить того, кто подключился
только что, а не показать давно отключившегося.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Protocol

import structlog

from app.services.connected_accounts import ConnectedAccounts


logger = structlog.get_logger(__name__)

#: Панель красит пользователя зелёным, пока с ``onlineAt`` прошло не больше минуты.
ONLINE_WINDOW = timedelta(seconds=60)
#: Максимум контракта ``GET /api/users``.
PAGE_SIZE = 1000
#: Потолок обхода: 10 000 подключённых одновременно — больше, чем держит любая панель бота.
MAX_PAGES = 10
#: Список и сегмент «Онлайн» открывают подряд — одного запроса к панели на 20 секунд хватает.
CACHE_TTL_SECONDS = 20.0
#: Список пользователей не должен ждать упавшую панель дольше этого.
PANEL_TIMEOUT_SECONDS = 8.0


class _PanelUser(Protocol):
    id: int
    telegram_id: int | None

    @property
    def online_at(self) -> datetime | None:
        """Когда аккаунт последний раз был подключён к VPN (по панели)."""


class _PanelUsersSource(Protocol):
    async def get_users_by_last_online(self, start: int, size: int) -> list[_PanelUser]:
        """Аккаунты панели по убыванию времени последнего подключения."""


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _freshest(marks: Mapping[int, datetime], key: int, moment: datetime) -> datetime:
    """Самая свежая из отметок одного ключа: у аккаунта их несколько (мультитариф)."""
    known = marks.get(key)
    return moment if known is None or moment > known else known


@dataclass(frozen=True)
class PanelOnlineSnapshot:
    """Отметки последнего подключения из панели: по её аккаунтам и по Telegram ID владельцев.

    Снимок отвечает на два вопроса строки списка: подключён ли человек прямо
    сейчас (``connected_now``) и когда он подключался в последний раз
    (``online_at_for`` — кабинет по ней сам гасит зелёную точку, не дожидаясь
    следующего ответа сервера).
    """

    by_panel_id: Mapping[int, datetime]
    by_telegram_id: Mapping[int, datetime]

    def online_at_for(self, user) -> datetime | None:
        """Самая свежая отметка среди аккаунтов пользователя; ``None`` — панель его не знает.

        Ключи те же, что у фильтра списка (``_users_list_conditions``): id панели у
        пользователя (одиночный тариф), у любой подписки (мультитариф) и Telegram ID.
        """
        marks = [
            mark
            for mark in (
                self.by_panel_id.get(user.remnawave_id),
                *(self.by_panel_id.get(sub.remnawave_id) for sub in user.subscriptions or ()),
                self.by_telegram_id.get(user.telegram_id),
            )
            if mark is not None
        ]
        return max(marks) if marks else None

    def connected_now(self, *, now: datetime | None = None, window: timedelta = ONLINE_WINDOW) -> ConnectedAccounts:
        """Кто подключён на момент ``now``: снимок мог быть взят на десятки секунд раньше."""
        since = (now or datetime.now(UTC)) - window
        return ConnectedAccounts(
            panel_ids=frozenset(key for key, mark in self.by_panel_id.items() if mark >= since),
            telegram_ids=frozenset(key for key, mark in self.by_telegram_id.items() if mark >= since),
        )


def _connected(users: Iterable[_PanelUser], since: datetime) -> tuple[list[_PanelUser], bool]:
    """Подключённые с начала страницы и признак, что дальше по списку только отключённые."""
    connected: list[_PanelUser] = []
    for user in users:
        online_at = user.online_at
        if online_at is None or _aware(online_at) < since:
            return connected, True
        connected.append(user)
    return connected, False


def _snapshot_of(users: Iterable[_PanelUser]) -> PanelOnlineSnapshot:
    by_panel_id: dict[int, datetime] = {}
    by_telegram_id: dict[int, datetime] = {}
    for user in users:
        online_at = user.online_at
        if online_at is None:
            continue
        moment = _aware(online_at)
        by_panel_id[user.id] = _freshest(by_panel_id, user.id, moment)
        if user.telegram_id:
            by_telegram_id[user.telegram_id] = _freshest(by_telegram_id, user.telegram_id, moment)
    return PanelOnlineSnapshot(
        by_panel_id=MappingProxyType(by_panel_id),
        by_telegram_id=MappingProxyType(by_telegram_id),
    )


async def fetch_online_snapshot(
    source: _PanelUsersSource,
    *,
    now: datetime | None = None,
    window: timedelta = ONLINE_WINDOW,
) -> PanelOnlineSnapshot:
    """Обойти список панели по убыванию ``onlineAt``, пока отметка свежее окна."""
    since = (now or datetime.now(UTC)) - window
    connected: list[_PanelUser] = []
    for page in range(MAX_PAGES):
        users = await source.get_users_by_last_online(start=page * PAGE_SIZE, size=PAGE_SIZE)
        fresh, reached_offline = _connected(users, since)
        connected.extend(fresh)
        if reached_offline or len(users) < PAGE_SIZE:
            break
    else:
        logger.warning('Список подключённых обрезан потолком обхода', max_accounts=MAX_PAGES * PAGE_SIZE)

    return _snapshot_of(connected)


async def _fetch_from_panel() -> PanelOnlineSnapshot | None:
    from app.services.remnawave_service import RemnaWaveService

    service = RemnaWaveService()
    if not service.is_configured:
        return None
    async with service.get_api_client() as api:
        return await fetch_online_snapshot(api)


@dataclass
class _Cache:
    """Последний ответ панели — удачный или «не знаем» — на ``CACHE_TTL_SECONDS``.

    Отказ тоже запоминается: иначе при лежащей панели каждое открытие списка ждало бы
    таймаут и повторы клиента заново.
    """

    stored_at: float = float('-inf')
    snapshot: PanelOnlineSnapshot | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def lookup(self) -> tuple[bool, PanelOnlineSnapshot | None]:
        return time.monotonic() - self.stored_at < CACHE_TTL_SECONDS, self.snapshot

    def store(self, snapshot: PanelOnlineSnapshot | None) -> PanelOnlineSnapshot | None:
        self.stored_at = time.monotonic()
        self.snapshot = snapshot
        return snapshot


_cache = _Cache()


async def get_online_snapshot() -> PanelOnlineSnapshot | None:
    """Отметки подключений; ``None`` — панель не настроена или не ответила (это «не знаем», не «никого»)."""
    cache = _cache
    hit, snapshot = cache.lookup()
    if hit:
        return snapshot
    async with cache.lock:
        hit, snapshot = cache.lookup()
        if hit:
            return snapshot
        try:
            fetched = await asyncio.wait_for(_fetch_from_panel(), timeout=PANEL_TIMEOUT_SECONDS)
        except Exception as error:
            logger.warning('Не удалось узнать у панели, кто подключён', error=str(error))
            fetched = None
        return cache.store(fetched)
