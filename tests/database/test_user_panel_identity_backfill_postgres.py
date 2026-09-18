"""Миграция 0125: аккаунт панели человеку из его единственной подписки — на PostgreSQL.

Аккаунты, созданные в мультитарифе, записывались только у подписки. После
возврата оператора в одиночный режим у таких людей «0 устройств», а покупка
заводила второй аккаунт. Миграция дописывает ``users.remnawave_id`` там, где
ответ однозначен: у человека ровно один аккаунт по всем его подпискам и этот
id не записан другому человеку (колонка уникальна).
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

from app.database.models import Base, Subscription, SubscriptionStatus, User
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres

TABLES = list(Base.metadata.sorted_tables)
MIGRATION = Path(__file__).resolve().parents[2] / 'migrations/alembic/versions/0125_user_panel_identity_backfill.py'
NOW = datetime.now(UTC)


async def _user(db, telegram_id: int, remnawave_id: int | None) -> User:
    user = User(telegram_id=telegram_id, first_name='Тест', language='ru', remnawave_id=remnawave_id)
    db.add(user)
    await db.flush()
    return user


def _subscription(
    user: User, *, panel_id: int | None, short_id: str, status: str = SubscriptionStatus.ACTIVE.value
) -> Subscription:
    return Subscription(
        user_id=user.id,
        status=status,
        is_trial=False,
        end_date=NOW + timedelta(days=30),
        remnawave_id=panel_id,
        remnawave_short_id=short_id,
    )


async def _run_migration(db) -> None:
    spec = importlib.util.spec_from_file_location('migration_0125', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def upgrade(sync_connection):
        with Operations.context(MigrationContext.configure(sync_connection)):
            module.upgrade()

    connection = await db.connection()
    await connection.run_sync(upgrade)


@pytest.mark.asyncio
async def test_migration_fills_user_account_only_when_unambiguous(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        one_account = await _user(db, 1, None)
        two_accounts = await _user(db, 2, None)
        already_set = await _user(db, 3, 30)
        holder = await _user(db, 4, 40)
        taken = await _user(db, 5, None)
        no_account = await _user(db, 6, None)
        db.add_all(
            [
                _subscription(one_account, panel_id=11, short_id='a1'),
                _subscription(
                    one_account, panel_id=None, short_id='a2'
                ),  # вторая строка без аккаунта — ответ всё ещё однозначен
                _subscription(two_accounts, panel_id=21, short_id='b1'),
                _subscription(two_accounts, panel_id=22, short_id='b2'),
                _subscription(already_set, panel_id=31, short_id='c1'),
                _subscription(taken, panel_id=40, short_id='d1'),  # id уже записан другому человеку
                _subscription(no_account, panel_id=None, short_id='e1'),
            ]
        )
        await db.commit()

        await _run_migration(db)
        await db.commit()
        for user in (one_account, two_accounts, already_set, holder, taken, no_account):
            await db.refresh(user)

        assert one_account.remnawave_id == 11
        assert two_accounts.remnawave_id is None, 'два разных аккаунта — не угадываем'
        assert already_set.remnawave_id == 30, 'записанный аккаунт не трогаем'
        assert holder.remnawave_id == 40
        assert taken.remnawave_id is None, 'id уже у другого человека — колонка уникальна'
        assert no_account.remnawave_id is None
