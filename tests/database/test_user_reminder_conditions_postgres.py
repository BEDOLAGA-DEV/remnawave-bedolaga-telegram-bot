"""Условия напоминаний на настоящем PostgreSQL.

Отчёт: создание напоминания с условием «способ входа» падало с «COALESCE types
bigint and character varying cannot be matched» — vk_id это BIGINT, а условие
сводило его с '' как строку. SQLite типы не сверяет, поэтому только здесь.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.database.models import Subscription, User
from app.services.user_reminders.conditions import ReminderConditions, condition_clauses
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
@pytest.mark.parametrize('auth', ['telegram_only', 'email_only', 'single_method'])
async def test_auth_condition_runs_on_postgres(postgres_database, auth):
    async with postgres_session(postgres_database, (User.__table__, Subscription.__table__)) as db:
        db.add_all(
            [
                User(telegram_id=None, vk_id=777001, auth_type='vk', username='vk_only', language='ru'),
                User(telegram_id=555001, username='tg_only', language='ru'),
            ]
        )
        await db.commit()

        clauses = condition_clauses(ReminderConditions(auth=auth), now=datetime.now(UTC))
        usernames = set((await db.execute(select(User.username).where(*clauses))).scalars())

    expected = {
        'telegram_only': {'tg_only'},
        'email_only': set(),
        'single_method': {'vk_only', 'tg_only'},
    }[auth]
    assert usernames == expected
