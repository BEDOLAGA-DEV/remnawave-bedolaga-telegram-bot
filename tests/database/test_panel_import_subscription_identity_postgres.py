"""Подписка, перенесённая из панели, знает свой аккаунт панели — на PostgreSQL.

Репорт (панель 3.4.4, бот 4.12.0): у людей, перенесённых в бота синхронизацией
из панели, кабинет и бот показывали 0 устройств, а у зарегистрированных через
бота всё было верно. Импорт одиночного режима писал id панели только в
``users.remnawave_id``, а ``subscriptions.remnawave_id`` оставлял пустым. Свой
путь бота (``push_subscription``) пишет оба. Мультитариф и все экраны по
выбранной подписке читают строго id подписки: пусто — устройств «нет».
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import select

from app.config import Settings
from app.database.models import Subscription, SubscriptionStatus, User
from app.services.remnawave_service import RemnaWaveService
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres

TABLES = [Subscription.__table__, User.__table__]
MIGRATION = (
    Path(__file__).resolve().parents[2] / 'migrations/alembic/versions/0124_subscription_panel_identity_backfill.py'
)
NOW = datetime.now(UTC)


def _panel_user(panel_id: int, telegram_id: int) -> dict:
    return {
        'id': panel_id,
        'telegramId': telegram_id,
        'shortUuid': f'short{panel_id}',
        'status': 'ACTIVE',
        'expireAt': (NOW + timedelta(days=30)).isoformat().replace('+00:00', 'Z'),
        'trafficLimitBytes': 0,
        'userTraffic': {'usedTrafficBytes': 0},
        'hwidDeviceLimit': 3,
        'activeInternalSquads': [],
        'subscriptionUrl': 'https://panel.example/sub',
    }


async def _user(db, telegram_id: int, remnawave_id: int | None) -> User:
    user = User(telegram_id=telegram_id, first_name='Тест', language='ru', remnawave_id=remnawave_id)
    db.add(user)
    await db.flush()
    return user


def _subscription(user: User, *, short_id: str, status: str = SubscriptionStatus.ACTIVE.value, **extra) -> Subscription:
    return Subscription(
        user_id=user.id,
        status=status,
        is_trial=extra.pop('is_trial', False),
        end_date=NOW + timedelta(days=30),
        remnawave_short_id=short_id,
        **extra,
    )


@pytest.fixture
def single_tariff(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)


@pytest.mark.usefixtures('single_tariff')
async def test_created_from_panel_carries_panel_id(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        user = await _user(db, 777001, remnawave_id=2)

        await RemnaWaveService()._create_subscription_from_panel_data(db, user, _panel_user(2, 777001))
        await db.commit()

        subscription = (await db.execute(select(Subscription))).scalar_one()
        assert subscription.remnawave_id == 2


@pytest.mark.usefixtures('single_tariff')
async def test_update_from_panel_binds_row_left_empty_by_old_import(postgres_database, monkeypatch):
    import app.services.grace_access_runtime as grace_runtime

    async def no_grace(_db):
        return set()

    monkeypatch.setattr(grace_runtime, 'get_open_grace_subscription_ids', no_grace)
    async with postgres_session(postgres_database, TABLES) as db:
        user = await _user(db, 777002, remnawave_id=5)
        db.add(_subscription(user, short_id='old5'))
        await db.commit()

        await RemnaWaveService()._update_subscription_from_panel_data(db, user, _panel_user(5, 777002))
        await db.commit()

        subscription = (await db.execute(select(Subscription))).scalar_one()
        assert subscription.remnawave_id == 5


async def _run_migration(db) -> None:
    spec = importlib.util.spec_from_file_location('migration_0124', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def upgrade(sync_connection):
        with Operations.context(MigrationContext.configure(sync_connection)):
            module.upgrade()

    connection = await db.connection()
    await connection.run_sync(upgrade)


async def test_migration_binds_only_unambiguous_rows(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        imported = await _user(db, 1, remnawave_id=11)
        two_subs = await _user(db, 2, remnawave_id=12)
        draft_only = await _user(db, 3, remnawave_id=13)
        taken = await _user(db, 4, remnawave_id=14)
        holder = await _user(db, 5, remnawave_id=None)
        no_panel = await _user(db, 6, remnawave_id=None)

        db.add_all(
            [
                _subscription(imported, short_id='a'),
                _subscription(two_subs, short_id='b1'),
                _subscription(two_subs, short_id='b2'),
                _subscription(draft_only, short_id='c', status=SubscriptionStatus.PENDING.value, is_trial=True),
                _subscription(taken, short_id='d'),
                _subscription(holder, short_id='e', remnawave_id=14),
                _subscription(no_panel, short_id='f'),
            ]
        )
        await db.commit()

        await _run_migration(db)
        await db.commit()

        rows = dict((await db.execute(select(Subscription.remnawave_short_id, Subscription.remnawave_id))).all())
        assert rows == {'a': 11, 'b1': None, 'b2': None, 'c': None, 'd': None, 'e': 14, 'f': None}
