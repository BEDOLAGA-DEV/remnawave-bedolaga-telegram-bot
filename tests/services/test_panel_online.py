"""«Онлайн» в админке — это подключение к VPN по панели, а не кнопки в боте.

Раньше сегмент «Онлайн» и зелёная точка в списке брали `User.last_activity`
(нажимал что-то в боте за 5 минут), а карточка — `userTraffic.onlineAt` панели.
Список и карточка спорили. Теперь подключённых берём у панели: список
пользователей, отсортированный по onlineAt, пока отметка свежее минуты.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.external.remnawave_api import RemnaWaveAPI
from app.services import panel_online
from app.services.panel_online import (
    ONLINE_WINDOW,
    ConnectedAccounts,
    PanelOnlineSnapshot,
    fetch_online_snapshot,
)


NOW = datetime(2026, 9, 14, 18, 0, tzinfo=UTC)


def _panel_user(panel_id: int, seconds_ago: float | None, telegram_id: int | None = None):
    online_at = None if seconds_ago is None else NOW - timedelta(seconds=seconds_ago)
    return SimpleNamespace(id=panel_id, telegram_id=telegram_id, online_at=online_at)


class _Pages:
    """Панель, отдающая заранее разложенные страницы и запоминающая запросы."""

    def __init__(self, users: list, page_size: int):
        self.users = users
        self.page_size = page_size
        self.calls: list[tuple[int, int]] = []

    async def get_users_by_last_online(self, start: int, size: int) -> list:
        self.calls.append((start, size))
        return self.users[start : start + size]


def _snapshot(by_panel_id: dict[int, datetime], by_telegram_id: dict[int, datetime] | None = None):
    return PanelOnlineSnapshot(by_panel_id=by_panel_id, by_telegram_id=by_telegram_id or {})


async def test_api_sorts_users_by_online_at_like_panel_table() -> None:
    api = RemnaWaveAPI(base_url='http://panel', api_key='key')
    api._make_request = AsyncMock(return_value={'response': {'users': [], 'total': 0}})

    await api.get_users_by_last_online(start=1000, size=5000)

    call = api._make_request.await_args
    assert call.args[:2] == ('GET', '/api/users')
    params = call.kwargs['params']
    assert params['start'] == 1000
    assert params['size'] == 1000  # контракт панели: size не больше 1000
    assert json.loads(params['sorting']) == [{'id': 'userTraffic.onlineAt', 'desc': True}]


async def test_collects_accounts_until_mark_is_older_than_window() -> None:
    panel = _Pages(
        [
            _panel_user(7, 5, telegram_id=111),
            _panel_user(8, 59),
            _panel_user(9, 61, telegram_id=333),  # вышел минуту назад — уже не онлайн
            _panel_user(10, 3600),
        ],
        page_size=1000,
    )

    snapshot = await fetch_online_snapshot(panel, now=NOW)

    assert snapshot.connected_now(now=NOW) == ConnectedAccounts(
        panel_ids=frozenset({7, 8}), telegram_ids=frozenset({111})
    )
    assert panel.calls == [(0, panel_online.PAGE_SIZE)]


async def test_never_connected_account_ends_the_list() -> None:
    panel = _Pages([_panel_user(1, 1), _panel_user(2, None), _panel_user(3, None)], page_size=1000)

    snapshot = await fetch_online_snapshot(panel, now=NOW)

    assert snapshot.connected_now(now=NOW).panel_ids == frozenset({1})


async def test_pages_through_while_whole_page_is_online(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(panel_online, 'PAGE_SIZE', 2)
    panel = _Pages([_panel_user(i, 1) for i in range(5)] + [_panel_user(99, 600)], page_size=2)

    snapshot = await fetch_online_snapshot(panel, now=NOW)

    assert snapshot.connected_now(now=NOW).panel_ids == frozenset(range(5))
    assert panel.calls == [(0, 2), (2, 2), (4, 2)]


async def test_page_limit_stops_runaway_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(panel_online, 'PAGE_SIZE', 2)
    monkeypatch.setattr(panel_online, 'MAX_PAGES', 2)
    panel = _Pages([_panel_user(i, 1) for i in range(10)], page_size=2)

    snapshot = await fetch_online_snapshot(panel, now=NOW)

    assert snapshot.connected_now(now=NOW).panel_ids == frozenset(range(4))
    assert len(panel.calls) == 2


def test_window_matches_panel_green_dot() -> None:
    # Панель красит пользователя зелёным, пока с onlineAt прошло не больше минуты.
    assert timedelta(seconds=60) == ONLINE_WINDOW


def test_user_is_connected_by_any_of_his_panel_keys() -> None:
    accounts = ConnectedAccounts(panel_ids=frozenset({7, 42}), telegram_ids=frozenset({555}))
    by_user_id = SimpleNamespace(remnawave_id=7, telegram_id=None, subscriptions=[])
    by_subscription = SimpleNamespace(
        remnawave_id=None, telegram_id=None, subscriptions=[SimpleNamespace(remnawave_id=42)]
    )
    by_telegram = SimpleNamespace(remnawave_id=None, telegram_id=555, subscriptions=[])
    offline = SimpleNamespace(remnawave_id=8, telegram_id=1, subscriptions=[SimpleNamespace(remnawave_id=9)])

    assert accounts.has_user(by_user_id)
    assert accounts.has_user(by_subscription)
    assert accounts.has_user(by_telegram)
    assert not accounts.has_user(offline)


async def test_cached_between_calls_and_none_when_panel_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fetched = _snapshot({1: NOW})
    fetch = AsyncMock(return_value=fetched)
    monkeypatch.setattr(panel_online, '_fetch_from_panel', fetch)
    monkeypatch.setattr(panel_online, '_cache', panel_online._Cache())

    assert await panel_online.get_online_snapshot() == fetched
    assert await panel_online.get_online_snapshot() == fetched
    assert fetch.await_count == 1

    monkeypatch.setattr(panel_online, '_cache', panel_online._Cache())
    monkeypatch.setattr(panel_online, '_fetch_from_panel', AsyncMock(side_effect=RuntimeError('502')))
    assert await panel_online.get_online_snapshot() is None


async def test_panel_failure_is_remembered_so_list_does_not_wait_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Панель лежит — список открывается сразу без отметок, а не ждёт таймаут на каждом открытии."""
    failing = AsyncMock(side_effect=RuntimeError('connection refused'))
    monkeypatch.setattr(panel_online, '_fetch_from_panel', failing)
    monkeypatch.setattr(panel_online, '_cache', panel_online._Cache())

    assert await panel_online.get_online_snapshot() is None
    assert await panel_online.get_online_snapshot() is None
    assert failing.await_count == 1


async def test_who_is_online_is_recounted_at_request_time_not_at_fetch_time() -> None:
    """Ответ панели живёт в кэше 20 секунд — «онлайн» за это время обязан гаснуть сам.

    Иначе человек, отметка которого была свежей в момент запроса к панели, ещё
    двадцать секунд числился бы в сети, хотя сама панель его уже погасила — ровно
    та «минута назад», из-за которой выборке не верили.
    """
    panel = _Pages([_panel_user(7, 5), _panel_user(8, 55)], page_size=1000)

    snapshot = await fetch_online_snapshot(panel, now=NOW)

    assert snapshot.connected_now(now=NOW).panel_ids == frozenset({7, 8})
    # Через десять секунд отметка восьмого перевалила за минуту — он уже не в сети.
    assert snapshot.connected_now(now=NOW + timedelta(seconds=10)).panel_ids == frozenset({7})
    # Ещё через минуту не в сети никто, хотя снимок тот же.
    assert snapshot.connected_now(now=NOW + timedelta(seconds=70)).panel_ids == frozenset()


async def test_snapshot_gives_each_row_its_own_mark() -> None:
    """Строке списка нужна отметка, а не готовое «да/нет»: по ней кабинет сам гасит точку."""
    panel = _Pages([_panel_user(7, 5, telegram_id=111), _panel_user(42, 30)], page_size=1000)

    snapshot = await fetch_online_snapshot(panel, now=NOW)

    by_panel = SimpleNamespace(remnawave_id=7, telegram_id=None, subscriptions=[])
    by_subscription = SimpleNamespace(
        remnawave_id=None, telegram_id=None, subscriptions=[SimpleNamespace(remnawave_id=42)]
    )
    by_telegram = SimpleNamespace(remnawave_id=None, telegram_id=111, subscriptions=[])
    unknown = SimpleNamespace(remnawave_id=9000, telegram_id=222, subscriptions=[])

    assert snapshot.online_at_for(by_panel) == NOW - timedelta(seconds=5)
    assert snapshot.online_at_for(by_subscription) == NOW - timedelta(seconds=30)
    assert snapshot.online_at_for(by_telegram) == NOW - timedelta(seconds=5)
    assert snapshot.online_at_for(unknown) is None


async def test_several_accounts_of_one_person_give_the_freshest_mark() -> None:
    """Мультитариф: у человека несколько аккаунтов панели — считается самый свежий."""
    panel = _Pages([_panel_user(7, 5, telegram_id=111), _panel_user(8, 50, telegram_id=111)], page_size=1000)

    snapshot = await fetch_online_snapshot(panel, now=NOW)

    person = SimpleNamespace(
        remnawave_id=None,
        telegram_id=111,
        subscriptions=[SimpleNamespace(remnawave_id=8), SimpleNamespace(remnawave_id=7)],
    )
    assert snapshot.online_at_for(person) == NOW - timedelta(seconds=5)
