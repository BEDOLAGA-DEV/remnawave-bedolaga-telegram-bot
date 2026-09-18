"""Сегмент «В грейсе» списка пользователей: только люди с ОТКРЫТЫМ временным доступом.

Сортировка по концу грейса сама по себе никого не выделяет, когда открытых
сессий нет: ключ пуст у всех, и список остаётся в порядке регистрации, что на
глаз выглядит как случайный набор людей без подписки. Сегмент отвечает прямо:
кто в грейсе сейчас, а если никого — пустой список. Закрытый грейс со старой
датой оверлея в сегмент не попадает: временного доступа у человека уже нет.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.database.crud.user import get_users_count, get_users_list
from tests.crud.test_users_list_filter_sort_matrix import TABLES, _subscription, _user
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres

NOW = datetime.now(UTC)


async def _seed(db) -> None:
    people = {
        'open_soon': (True, NOW + timedelta(hours=5)),
        'open_late': (True, NOW + timedelta(days=2)),
        'closed': (False, NOW - timedelta(days=1)),
        'never': (False, None),
    }
    for rank, (name, (is_open, until)) in enumerate(people.items(), start=1):
        user = _user(300 + rank, name)
        db.add(user)
        await db.flush()
        sub = _subscription(user, name)
        sub.grace_session_open = is_open
        sub.grace_overlay_expire_at = until
        db.add(sub)
    await db.commit()


async def test_segment_lists_only_people_with_open_grace_soonest_first(postgres_database: str) -> None:
    async with postgres_session(postgres_database, list(TABLES)) as db:
        await _seed(db)

        users = await get_users_list(db, in_grace=True, order_by_grace=True)
        assert [u.username for u in users] == ['open_soon', 'open_late']
        assert await get_users_count(db, in_grace=True) == 2


async def test_segment_can_be_inverted_and_leaves_everyone_alone_when_unset(postgres_database: str) -> None:
    async with postgres_session(postgres_database, list(TABLES)) as db:
        await _seed(db)

        outside = await get_users_list(db, in_grace=False)
        assert {u.username for u in outside} == {'closed', 'never'}
        assert await get_users_count(db, in_grace=False) == 2

        assert await get_users_count(db) == 4
