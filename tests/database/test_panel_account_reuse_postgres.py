"""Перед синхронизацией с панелью: создавать новый аккаунт или обновлять свой.

Вопрос владельца (18.09): «если человек перейдёт с классики на тариф, в панели
создастся новый аккаунт?». Не должен: у человека остаётся его ссылка. В
мультитарифе аккаунт панели адресуется через ``subscriptions.remnawave_id``, а у
подписок из одиночного режима он мог остаться пустым (миграция 0124 привязывала
только при одной строке у человека). Тогда каждая точка «create или update»
заводила ВТОРОЙ аккаунт. Правило одно на все точки: если у строки id нет, а у
человека есть и его не держит другая подписка — привязываем и обновляем.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.database.models import Base, Subscription, SubscriptionStatus, User
from app.services.panel_sync import should_create_panel_account
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres

TABLES = list(Base.metadata.sorted_tables)
PANEL_ID = 555


@pytest.fixture
def multi_tariff(monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)


@pytest.fixture
def single_tariff(monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)


def _subscription(user_id: int, *, panel_id: int | None, short_id: str) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        user_id=user_id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        tariff_id=None,
        start_date=now - timedelta(days=5),
        end_date=now + timedelta(days=10),
        remnawave_id=panel_id,
        remnawave_short_id=short_id,
    )


async def _seed(db, *, user_panel_id: int | None, sub_panel_id: int | None) -> tuple[User, Subscription]:
    user = User(telegram_id=1001, first_name='Старый', language='ru', status='active', remnawave_id=user_panel_id)
    db.add(user)
    await db.flush()
    sub = _subscription(user.id, panel_id=sub_panel_id, short_id='sub-a')
    db.add(sub)
    await db.commit()
    return user, sub


@pytest.mark.asyncio
async def test_multi_tariff_row_with_panel_id_is_updated(postgres_database, multi_tariff):
    async with postgres_session(postgres_database, TABLES) as db:
        user, sub = await _seed(db, user_panel_id=PANEL_ID, sub_panel_id=PANEL_ID)

        assert await should_create_panel_account(db, sub, user) is False


@pytest.mark.asyncio
async def test_multi_tariff_row_without_id_adopts_free_user_account(postgres_database, multi_tariff):
    """Старая подписка без id, аккаунт у человека есть и свободен — привязать и обновить, не создавать."""
    async with postgres_session(postgres_database, TABLES) as db:
        user, sub = await _seed(db, user_panel_id=PANEL_ID, sub_panel_id=None)

        assert await should_create_panel_account(db, sub, user) is False
        assert sub.remnawave_id == PANEL_ID


@pytest.mark.asyncio
async def test_multi_tariff_row_without_id_creates_when_user_account_is_taken(postgres_database, multi_tariff):
    """Аккаунт человека уже у другой подписки — новой строке нужен свой аккаунт."""
    async with postgres_session(postgres_database, TABLES) as db:
        user, sub = await _seed(db, user_panel_id=PANEL_ID, sub_panel_id=None)
        db.add(_subscription(user.id, panel_id=PANEL_ID, short_id='sub-b'))
        await db.commit()

        assert await should_create_panel_account(db, sub, user) is True
        assert sub.remnawave_id is None


@pytest.mark.asyncio
async def test_multi_tariff_row_without_any_account_creates(postgres_database, multi_tariff):
    async with postgres_session(postgres_database, TABLES) as db:
        user, sub = await _seed(db, user_panel_id=None, sub_panel_id=None)

        assert await should_create_panel_account(db, sub, user) is True


@pytest.mark.asyncio
async def test_single_tariff_follows_user_account_only(postgres_database, single_tariff):
    async with postgres_session(postgres_database, TABLES) as db:
        user, sub = await _seed(db, user_panel_id=PANEL_ID, sub_panel_id=None)
        assert await should_create_panel_account(db, sub, user) is False

        user.remnawave_id = None
        assert await should_create_panel_account(db, sub, user) is True


@pytest.mark.asyncio
async def test_single_tariff_adopts_subscription_account_when_user_has_none(postgres_database, single_tariff):
    """Аккаунт создан в мультитарифе (записан у подписки), оператор вернулся в одиночный режим:
    покупка обновляет этот аккаунт и записывает его человеку, а не заводит новый."""
    async with postgres_session(postgres_database, TABLES) as db:
        user, sub = await _seed(db, user_panel_id=None, sub_panel_id=PANEL_ID)

        assert await should_create_panel_account(db, sub, user) is False
        assert user.remnawave_id == PANEL_ID


@pytest.mark.asyncio
async def test_single_tariff_does_not_steal_account_held_by_another_user(postgres_database, single_tariff):
    async with postgres_session(postgres_database, TABLES) as db:
        user, sub = await _seed(db, user_panel_id=None, sub_panel_id=PANEL_ID)
        db.add(User(telegram_id=1002, first_name='Другой', language='ru', status='active', remnawave_id=PANEL_ID))
        await db.commit()

        assert await should_create_panel_account(db, sub, user) is True
        assert user.remnawave_id is None
