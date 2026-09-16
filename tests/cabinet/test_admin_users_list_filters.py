"""Фильтры списка пользователей для сегментов кабинета.

Раздел «Пользователи» в кабинете получил готовые выборки: «Истекают за 7 дней»,
«Онлайн», «Без покупок», «Без подписки», «С ограничениями», а поиск стал одним
полем. Список и счётчик обязаны фильтровать одинаково — иначе «показано 12 из 40»
врёт, а лента не знает, когда остановиться.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.database.crud.user import get_users_count, get_users_list
from app.database.models import (
    Subscription,
    SubscriptionStatus,
    Tariff,
    Transaction,
    TransactionType,
    User,
    UserStatus,
)
from app.services.panel_online import ConnectedAccounts, PanelOnlineSnapshot
from tests.fixtures.sqlite_memory import memory_session


TABLES = (User.__table__, Subscription.__table__, Tariff.__table__, Transaction.__table__)
NOW = datetime.now(UTC)


def _user(telegram_id: int, username: str, **extra) -> User:
    return User(
        telegram_id=telegram_id,
        username=username,
        first_name=username.capitalize(),
        status=UserStatus.ACTIVE.value,
        language='ru',
        balance_kopeks=0,
        **extra,
    )


def _subscription(user: User, days_left: int, status: str = SubscriptionStatus.ACTIVE.value) -> Subscription:
    return Subscription(
        user_id=user.id,
        status=status,
        start_date=NOW - timedelta(days=20),
        end_date=NOW + timedelta(days=days_left),
        traffic_limit_gb=100,
        device_limit=1,
        # Колонка уникальна; по умолчанию генератор даёт одно и то же на SQLite.
        remnawave_short_id=f'short{user.id}',
    )


async def _seed(db) -> None:
    soon = _user(1, 'soon', last_activity=NOW - timedelta(minutes=2), email='soon@example.com')
    later = _user(2, 'later', last_activity=NOW - timedelta(hours=3), restriction_topup=True)
    # last_activity по умолчанию ставится «сейчас», поэтому давность задаём явно.
    nobody = _user(3, 'nobody', last_activity=NOW - timedelta(days=30))
    lapsed = _user(4, 'lapsed', last_activity=NOW - timedelta(days=9))
    db.add_all([soon, later, nobody, lapsed])
    await db.flush()
    db.add_all(
        [
            _subscription(soon, days_left=3),
            _subscription(later, days_left=40),
            # Истёкшая неделю назад в «истекают» не попадает, хоть дата и близко.
            _subscription(lapsed, days_left=-7, status=SubscriptionStatus.EXPIRED.value),
            Transaction(
                user_id=later.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT.value,
                amount_kopeks=-50000,
                description='Покупка',
                is_completed=True,
            ),
        ]
    )
    await db.commit()


async def _usernames(db, **filters) -> list[str]:
    return sorted(str(u.username) for u in await get_users_list(db, **filters))


async def test_expires_within_days(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        assert await _usernames(db, expires_within_days=7) == ['soon']
        assert await get_users_count(db, expires_within_days=7) == 1


async def test_active_within_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        assert await _usernames(db, active_within_minutes=5) == ['soon']
        assert await get_users_count(db, active_within_minutes=5) == 1


async def test_has_restrictions(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        assert await _usernames(db, has_restrictions=True) == ['later']
        assert await get_users_count(db, has_restrictions=True) == 1
        assert await _usernames(db, has_restrictions=False) == ['lapsed', 'nobody', 'soon']


async def test_has_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        assert await _usernames(db, has_subscription=False) == ['nobody']
        assert await get_users_count(db, has_subscription=False) == 1
        assert await _usernames(db, has_subscription=True) == ['lapsed', 'later', 'soon']


async def test_no_purchases(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        assert await _usernames(db, purchase_count=0) == ['lapsed', 'nobody', 'soon']
        assert await get_users_count(db, purchase_count=0) == 3


async def test_search_matches_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """Одно поле поиска: адрес целиком и его кусок находят человека через `search`."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        assert await _usernames(db, search='soon@example.com') == ['soon']
        assert await _usernames(db, search='example.com') == ['soon']
        assert await get_users_count(db, search='example.com') == 1


async def test_traffic_used_percent_min(monkeypatch: pytest.MonkeyPatch) -> None:
    """«Трафик на исходе»: израсходовано от N % лимита, исчерпанные тоже; безлимит и истёкшие — нет."""
    async with memory_session(monkeypatch, TABLES) as db:
        heavy = _user(11, 'heavy')
        done = _user(12, 'done')
        light = _user(13, 'light')
        unlimited = _user(14, 'unlimited')
        gone = _user(15, 'gone')
        db.add_all([heavy, done, light, unlimited, gone])
        await db.flush()
        rows = [
            (heavy, 85.0, 100, SubscriptionStatus.ACTIVE.value),
            (done, 100.0, 100, SubscriptionStatus.LIMITED.value),
            (light, 20.0, 100, SubscriptionStatus.ACTIVE.value),
            (unlimited, 900.0, 0, SubscriptionStatus.ACTIVE.value),
            (gone, 95.0, 100, SubscriptionStatus.EXPIRED.value),
        ]
        for user, used, limit, status in rows:
            sub = _subscription(user, days_left=10, status=status)
            sub.traffic_used_gb = used
            sub.traffic_limit_gb = limit
            db.add(sub)
        await db.commit()

        assert await _usernames(db, traffic_used_percent_min=80) == ['done', 'heavy']
        assert await get_users_count(db, traffic_used_percent_min=80) == 2


async def test_connected_now_matches_any_panel_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """«Онлайн» = подключён к VPN: id панели у пользователя, у подписки или Telegram ID аккаунта."""
    async with memory_session(monkeypatch, TABLES) as db:
        by_user = _user(21, 'by_user', remnawave_id=7001)
        by_sub = _user(22, 'by_sub')
        by_telegram = _user(23, 'by_telegram')
        offline = _user(24, 'offline', remnawave_id=7002)
        db.add_all([by_user, by_sub, by_telegram, offline])
        await db.flush()
        sub = _subscription(by_sub, days_left=10)
        sub.remnawave_id = 7003
        db.add(sub)
        await db.commit()

        connected = ConnectedAccounts(panel_ids=frozenset({7001, 7003}), telegram_ids=frozenset({23}))
        assert await _usernames(db, connected=connected) == ['by_sub', 'by_telegram', 'by_user']
        assert await get_users_count(db, connected=connected) == 3

        nobody = ConnectedAccounts(panel_ids=frozenset(), telegram_ids=frozenset())
        assert await _usernames(db, connected=nobody) == []
        assert await get_users_count(db, connected=nobody) == 0


async def _list(db, **params):
    from app.cabinet.routes import admin_users

    defaults = {
        'offset': 0,
        'limit': 50,
        'search': None,
        'email': None,
        'status': None,
        'subscription_status': None,
        'tariff_id': None,
        'promo_group_id': None,
        'campaign_id': None,
        'partner_id': None,
        'expires_within_days': None,
        'active_within_minutes': None,
        'has_restrictions': None,
        'has_subscription': None,
        'purchase_count': None,
        'traffic_used_percent_min': None,
        'online': None,
        'sort_by': admin_users.SortByEnum.CREATED_AT,
    }
    return await admin_users.list_users(**{**defaults, **params}, admin=None, db=db)


async def test_route_marks_connected_rows_and_filters_online(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import panel_online

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        snapshot = PanelOnlineSnapshot(by_panel_id={}, by_telegram_id={2: NOW})
        monkeypatch.setattr(panel_online, 'get_online_snapshot', AsyncMock(return_value=snapshot))

        everyone = await _list(db)
        assert {row.username: row.is_online for row in everyone.users} == {
            'soon': False,
            'later': True,
            'nobody': False,
            'lapsed': False,
        }
        # Строка несёт и саму отметку: кабинет гасит точку по ней, не дожидаясь сервера.
        assert {row.username: row.online_at for row in everyone.users} == {
            'soon': None,
            'later': NOW,
            'nobody': None,
            'lapsed': None,
        }

        only_online = await _list(db, online=True)
        assert [row.username for row in only_online.users] == ['later']
        assert only_online.total == 1


async def test_route_refuses_online_filter_without_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Панель молчит — «онлайн» не угадываем и не отдаём всех: честная ошибка, а строки без отметки."""
    from fastapi import HTTPException

    from app.services import panel_online

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        monkeypatch.setattr(panel_online, 'get_online_snapshot', AsyncMock(return_value=None))

        with pytest.raises(HTTPException) as refused:
            await _list(db, online=True)
        assert refused.value.status_code == 503

        everyone = await _list(db)
        assert len(everyone.users) == 4
        assert {row.is_online for row in everyone.users} == {None}


async def test_filters_combine(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        assert await _usernames(db, expires_within_days=7, active_within_minutes=5) == ['soon']
        assert await _usernames(db, expires_within_days=7, has_restrictions=True) == []


def test_route_declares_new_filters() -> None:
    from app.cabinet.routes.admin_users import list_users

    params = set(inspect.signature(list_users).parameters)
    assert {
        'expires_within_days',
        'active_within_minutes',
        'has_restrictions',
        'has_subscription',
        'purchase_count',
        'traffic_used_percent_min',
        'online',
    } <= params
