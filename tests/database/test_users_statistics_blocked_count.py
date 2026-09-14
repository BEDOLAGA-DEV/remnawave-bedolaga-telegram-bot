"""Сводка пользователей: «заблокировано» — это статус «заблокирован», а не «все, кто не активен».

Жалоба владельца (кабинет, 11.09.2026): карточка «Заблокировано: 1097», а в списке с фильтром
«Заблокированные» одна страница. Сводка считала «всего минус активные» — туда попадали и удалённые
пользователи, которых в разы больше. Список фильтрует по настоящему статусу, сводка теперь тоже.
"""

from __future__ import annotations

import pytest

from app.database.crud.user import get_users_statistics
from app.database.models import User
from tests.fixtures.sqlite_memory import memory_session


TABLES = [User.__table__]


def _user(user_id: int, status: str) -> User:
    return User(
        id=user_id,
        telegram_id=1000 + user_id,
        first_name=f'U{user_id}',
        language='ru',
        status=status,
        balance_kopeks=0,
    )


@pytest.mark.asyncio
async def test_blocked_counts_only_the_blocked_status(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                *(_user(i, 'active') for i in range(1, 4)),
                *(_user(i, 'blocked') for i in range(4, 6)),
                *(_user(i, 'deleted') for i in range(6, 10)),
            ]
        )
        await db.commit()
        stats = await get_users_statistics(db)

    assert stats['total_users'] == 9
    assert stats['active_users'] == 3
    assert stats['blocked_users'] == 2, 'удалённые — не заблокированные'
    assert stats['deleted_users'] == 4
