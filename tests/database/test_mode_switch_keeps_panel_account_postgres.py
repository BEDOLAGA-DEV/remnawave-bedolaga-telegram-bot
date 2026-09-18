"""Переключение режима продаж не теряет аккаунт панели — на PostgreSQL, боевой сервис.

Вопрос владельца (18.09): «если не было мультитарифа, а были тарифы — нулей по
устройствам не будет? и наоборот?». Аккаунт создаёт боевой ``create_remnawave_user``
(панель — фейк, только API-вызовы), потом режим переключается, и оба резолвера
адреса панели (кабинет и бот) обязаны найти тот же аккаунт: без аккаунта экран
устройств показывает «0 устройств».
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.cabinet.routes.subscription_modules.devices import _resolve_panel_user_id
from app.config import Settings, settings
from app.database.models import Base, Subscription, SubscriptionStatus, User
from app.handlers.subscription.devices import _get_panel_user_id
from app.services.subscription_service import SubscriptionService
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres

TABLES = list(Base.metadata.sorted_tables)
NEW_PANEL_ID = 7002


class _PanelApi:
    """Панель, в которой людей ещё нет: create заводит аккаунт NEW_PANEL_ID."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.updated: list[int] = []

    async def get_user_by_id(self, panel_user_id):
        return SimpleNamespace(id=panel_user_id) if panel_user_id == NEW_PANEL_ID and self.created else None

    async def get_user_by_short_uuid(self, _short_uuid):
        return None

    async def find_users_by_telegram_id(self, _telegram_id):
        return []

    async def find_users_by_email(self, _email):
        return []

    async def create_user(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(
            id=NEW_PANEL_ID, short_uuid='s2', subscription_url='https://p.example/new', happ_crypto_link='c2'
        )

    async def update_user(self, panel_user_id=None, **kwargs):
        self.updated.append(panel_user_id)
        return SimpleNamespace(
            id=panel_user_id, short_uuid='s2', subscription_url='https://p.example/new', happ_crypto_link='c2'
        )

    async def reset_user_devices(self, *_args, **_kwargs):
        return True


def _multi(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: value)


@pytest.fixture
def panel(monkeypatch) -> _PanelApi:
    api = _PanelApi()

    @asynccontextmanager
    async def fake_api_client(self):
        yield api

    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(SubscriptionService, 'get_api_client', fake_api_client)
    monkeypatch.setattr(SubscriptionService, 'validate_and_clean_subscription', AsyncMock(return_value=True))
    monkeypatch.setattr('app.services.panel_sync.payload.get_traffic_reset_strategy', lambda _tariff: 'NO_RESET')
    monkeypatch.setattr('app.utils.subscription_utils.resolve_hwid_device_limit_for_payload', lambda _sub: None)
    return api


async def _seed(db) -> Subscription:
    now = datetime.now(UTC)
    user = User(telegram_id=1001, first_name='Тест', username='tester', language='ru', status='active')
    db.add(user)
    await db.flush()
    db.add(
        Subscription(
            user_id=user.id,
            status=SubscriptionStatus.ACTIVE.value,
            is_trial=False,
            tariff_id=None,
            start_date=now,
            end_date=now + timedelta(days=30),
            traffic_limit_gb=100,
            device_limit=3,
            connected_squads=['squad-1'],
            remnawave_short_id='mode-switch',
        )
    )
    await db.commit()
    return (
        await db.execute(
            select(Subscription).options(selectinload(Subscription.user), selectinload(Subscription.tariff))
        )
    ).scalar_one()


async def _reload(db) -> tuple[Subscription, User]:
    sub = (
        await db.execute(
            select(Subscription).options(selectinload(Subscription.user)).execution_options(populate_existing=True)
        )
    ).scalar_one()
    return sub, sub.user


def _both_resolvers_find(sub, user) -> None:
    assert _resolve_panel_user_id(sub, user) == NEW_PANEL_ID, 'кабинет: аккаунт не найден — будет «0 устройств»'
    assert _get_panel_user_id(sub, user) == NEW_PANEL_ID, 'бот: аккаунт не найден — будет «0 устройств»'


@pytest.mark.asyncio
async def test_account_created_in_single_mode_survives_switch_to_multi(postgres_database, panel, monkeypatch):
    async with postgres_session(postgres_database, TABLES) as db:
        sub = await _seed(db)
        _multi(monkeypatch, False)

        await SubscriptionService().create_remnawave_user(db, sub)

        sub, user = await _reload(db)
        assert len(panel.created) == 1, 'в панели должен появиться ровно один аккаунт'
        assert (user.remnawave_id, sub.remnawave_id) == (NEW_PANEL_ID, NEW_PANEL_ID)
        _both_resolvers_find(sub, user)

        _multi(monkeypatch, True)
        _both_resolvers_find(sub, user)


@pytest.mark.asyncio
async def test_account_created_in_multi_mode_survives_switch_to_single(postgres_database, panel, monkeypatch):
    async with postgres_session(postgres_database, TABLES) as db:
        sub = await _seed(db)
        _multi(monkeypatch, True)

        await SubscriptionService().create_remnawave_user(db, sub)

        sub, user = await _reload(db)
        assert len(panel.created) == 1
        assert sub.remnawave_id == NEW_PANEL_ID
        assert user.remnawave_id == NEW_PANEL_ID, 'аккаунт из мультитарифа должен быть записан и человеку'
        _both_resolvers_find(sub, user)

        _multi(monkeypatch, False)
        _both_resolvers_find(sub, user)


@pytest.mark.asyncio
async def test_old_multi_rows_without_user_account_still_resolve_in_single_mode(postgres_database, monkeypatch):
    """Строки, созданные ДО правки: аккаунт только у подписки. Одиночный режим всё равно находит его."""
    async with postgres_session(postgres_database, TABLES) as db:
        sub = await _seed(db)
        sub.remnawave_id = NEW_PANEL_ID
        await db.commit()
        sub, user = await _reload(db)
        assert user.remnawave_id is None

        _multi(monkeypatch, False)
        _both_resolvers_find(sub, user)
