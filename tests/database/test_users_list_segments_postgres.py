"""Сегменты списка пользователей возвращают ПРАВИЛЬНЫХ людей — на PostgreSQL.

Владелец (18.09): «есть фильтр триал, но если триал настроен не через тариф, а
по классике — фильтр не работает; реши уже вопрос с этими фильтрами».

По коду фильтр `subscription_status=trial` искал строки со статусом ``trial``,
а триалы (и классические, и тарифные) создаются со статусом ``active`` и
признаком ``is_trial`` — сегмент был пуст для всех триалов, зато «Активные»
их включали. Прежняя матрица тестов проверяла лишь, что запросы не падают,
а не кого они возвращают. Здесь — кого именно: для каждого сегмента ровно
ожидаемое множество людей, список и счётчик сходятся, сортировка по дате
окончания никого не теряет.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.database.crud.user import get_users_count, get_users_list
from app.database.models import Base, Subscription, SubscriptionStatus, Tariff, User, UserStatus
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres

TABLES = list(Base.metadata.sorted_tables)
NOW = datetime.now(UTC)
EXPIRING_DAYS = 7


def _user(telegram_id: int, username: str) -> User:
    return User(
        telegram_id=telegram_id,
        username=username,
        first_name=username,
        status=UserStatus.ACTIVE.value,
        language='ru',
        balance_kopeks=0,
    )


def _sub(user: User, *, days_left: float, status: str = SubscriptionStatus.ACTIVE.value, **extra) -> Subscription:
    return Subscription(
        user_id=user.id,
        status=status,
        is_trial=extra.pop('is_trial', False),
        tariff_id=extra.pop('tariff_id', None),
        start_date=NOW - timedelta(days=20),
        end_date=NOW + timedelta(days=days_left),
        traffic_limit_gb=100,
        device_limit=1,
        remnawave_short_id=f'short-{user.telegram_id}',
        **extra,
    )


async def _seed(db) -> dict[str, int]:
    """Возвращает {имя сценария: user.id}."""
    tariff = Tariff(
        name='Пробный',
        description='',
        is_active=True,
        is_daily=False,
        period_prices={'30': 30000},
        traffic_limit_gb=100,
        device_limit=1,
        display_order=1,
        is_trial_available=True,
    )
    db.add(tariff)
    people = {
        'classic_trial': _user(1, 'classic_trial'),
        'tariff_trial': _user(2, 'tariff_trial'),
        'paid_active': _user(3, 'paid_active'),
        'paid_expiring': _user(4, 'paid_expiring'),
        'paid_overdue': _user(5, 'paid_overdue'),  # статус active, но срок прошёл — монитор ещё не переставил
        'expired': _user(6, 'expired'),
        'limited': _user(7, 'limited'),
        'disabled': _user(8, 'disabled'),
        'trial_overdue': _user(9, 'trial_overdue'),  # триал, срок прошёл
        'no_subscription': _user(10, 'no_subscription'),
    }
    db.add_all(people.values())
    await db.flush()
    db.add_all(
        [
            _sub(people['classic_trial'], days_left=3, is_trial=True),
            _sub(people['tariff_trial'], days_left=3, is_trial=True, tariff_id=tariff.id),
            _sub(people['paid_active'], days_left=30),
            _sub(people['paid_expiring'], days_left=2),
            _sub(people['paid_overdue'], days_left=-1),
            _sub(people['expired'], days_left=-5, status=SubscriptionStatus.EXPIRED.value),
            _sub(people['limited'], days_left=10, status=SubscriptionStatus.LIMITED.value),
            _sub(people['disabled'], days_left=10, status=SubscriptionStatus.DISABLED.value),
            _sub(people['trial_overdue'], days_left=-1, is_trial=True),
        ]
    )
    await db.commit()
    return {name: int(user.id) for name, user in people.items()}


async def _segment(db, **params) -> set[int]:
    users = await get_users_list(db, limit=100, **params)
    count = await get_users_count(db, **params)
    ids = {int(user.id) for user in users}
    assert count == len(ids), f'счётчик {count} не сходится со списком {len(ids)}: {params}'
    return ids


EXPECTED = {
    'trial': {'classic_trial', 'tariff_trial'},
    'active': {'paid_active', 'paid_expiring'},
    'expired': {'paid_overdue', 'expired', 'trial_overdue'},
    'limited': {'limited'},
    'disabled': {'disabled'},
}


@pytest.mark.asyncio
@pytest.mark.parametrize('segment', sorted(EXPECTED))
async def test_segment_returns_exactly_its_people(postgres_database, segment):
    async with postgres_session(postgres_database, TABLES) as db:
        ids = await _seed(db)

        got = await _segment(db, subscription_status=segment)

        assert got == {ids[name] for name in EXPECTED[segment]}, (
            f'сегмент {segment}: {sorted(name for name, uid in ids.items() if uid in got)}'
        )


@pytest.mark.asyncio
async def test_expiring_segment_is_paid_only(postgres_database):
    """«Истекают за 7 дней» — платные, у которых срок скоро; триалы и просроченные не сюда."""
    async with postgres_session(postgres_database, TABLES) as db:
        ids = await _seed(db)

        got = await _segment(db, subscription_status='active', expires_within_days=EXPIRING_DAYS)

        assert got == {ids['paid_expiring']}


@pytest.mark.asyncio
async def test_no_subscription_segment(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        ids = await _seed(db)

        got = await _segment(db, has_subscription=False)

        assert got == {ids['no_subscription']}


@pytest.mark.asyncio
@pytest.mark.parametrize('segment', sorted(EXPECTED))
async def test_sorting_by_end_date_keeps_every_person_of_the_segment(postgres_database, segment):
    async with postgres_session(postgres_database, TABLES) as db:
        ids = await _seed(db)

        users = await get_users_list(db, limit=100, subscription_status=segment, order_by_subscription_end=True)

        assert {int(user.id) for user in users} == {ids[name] for name in EXPECTED[segment]}


async def _rows(db, monkeypatch, **params):
    """Строки так, как их отдаёт ручка кабинета (панель не опрашиваем)."""
    from unittest.mock import AsyncMock

    from app.cabinet.routes import admin_users
    from app.services import panel_online

    monkeypatch.setattr(panel_online, 'get_online_snapshot', AsyncMock(return_value=None))
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
        'in_grace': None,
        'sort_by': admin_users.SortByEnum.CREATED_AT,
        'sort_order': None,
    }
    response = await admin_users.list_users(**{**defaults, **params}, admin=None, db=db)
    return response.users


@pytest.mark.asyncio
async def test_route_rows_carry_the_segment_for_the_chip(postgres_database, monkeypatch):
    """Чип строки читает subscription_status: у классического триала он «trial», у просроченного — «expired»."""
    async with postgres_session(postgres_database, TABLES) as db:
        await _seed(db)

        trial_rows = await _rows(db, monkeypatch, subscription_status='trial')
        assert sorted(r.username for r in trial_rows) == ['classic_trial', 'tariff_trial']
        assert {r.subscription_status for r in trial_rows} == {'trial'}
        assert all(r.subscription_is_trial for r in trial_rows)

        expired_rows = await _rows(db, monkeypatch, subscription_status='expired')
        assert sorted(r.username for r in expired_rows) == ['expired', 'paid_overdue', 'trial_overdue']
        assert {r.subscription_status for r in expired_rows} == {'expired'}

        active_rows = await _rows(db, monkeypatch, subscription_status='active')
        assert sorted(r.username for r in active_rows) == ['paid_active', 'paid_expiring']
        assert {r.subscription_status for r in active_rows} == {'active'}


@pytest.mark.asyncio
async def test_stat_cards_count_the_same_segments(postgres_database):
    """Плитки над списком считают по тем же правилам, что и выборки."""
    from app.cabinet.routes import admin_users

    async with postgres_session(postgres_database, TABLES) as db:
        await _seed(db)

        stats = await admin_users.get_users_stats(admin=None, db=db)

        assert (
            stats.users_with_trial,
            stats.users_with_active_subscription,
            stats.users_with_expired_subscription,
        ) == (2, 2, 3)
