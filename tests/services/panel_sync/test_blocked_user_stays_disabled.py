"""Заблокированный пользователь не оживает от синхронизации.

Проверено на живом коде до правки: массовая синхронизация «Из бота в панель»
отправляла ``UserStatus.ACTIVE`` пользователю со статусом ``blocked``, у которого
строка подписки ещё активна, — то есть возвращала ему доступ. Блокировку
возвращал обратно только фоновый мониторинг, до следующего нажатия кнопки.

Причина была в том, что определение «жива ли подписка» существовало в пяти
копиях: сервис подписок смотрел на статус пользователя, а массовая синхронизация
и обе кнопки кабинета — только на колонку подписки.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.database.crud.subscription as crud_sub_mod
import app.services.grace_access_runtime as grace_runtime_mod
from app.cabinet.routes import admin_users
from app.config import Settings
from app.database.models import SubscriptionStatus
from app.external.remnawave_api import UserStatus
from app.services.remnawave_service import RemnaWaveService


PANEL_ID = 4242


def _blocked_user() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        telegram_id=555,
        username='u',
        full_name='User',
        email=None,
        remnawave_id=PANEL_ID,
        status='blocked',
        last_remnawave_sync=None,
        updated_at=None,
        subscriptions=[],
    )


def _live_subscription() -> SimpleNamespace:
    return SimpleNamespace(
        id=500,
        user_id=1,
        status=SubscriptionStatus.ACTIVE.value,
        is_active=True,
        end_date=datetime.now(UTC) + timedelta(days=30),
        traffic_limit_gb=50,
        connected_squads=[],
        tariff=None,
        remnawave_id=PANEL_ID,
        remnawave_short_uuid='aBcD12',
        remnawave_short_id='sid500',
        subscription_url='',
        subscription_crypto_link='',
        device_limit=1,
    )


@pytest.mark.asyncio
async def test_bulk_sync_does_not_reactivate_a_blocked_user(monkeypatch):
    subscription = _live_subscription()
    subscription.user = _blocked_user()
    api = AsyncMock()
    api.update_user.return_value = SimpleNamespace(
        id=PANEL_ID,
        short_uuid='aBcD12',
        subscription_url='https://s/aBcD12',
        happ_crypto_link=None,
        expire_at=subscription.end_date,
    )
    batches = [[subscription], []]

    async def fake_batch(db, offset=0, limit=500):
        return batches.pop(0) if batches else []

    @asynccontextmanager
    async def fake_lease(subscription_id):
        yield SimpleNamespace(allowed=True, subscription=subscription, has_open_grace=False)

    monkeypatch.setattr(crud_sub_mod, 'get_subscriptions_batch', fake_batch)
    monkeypatch.setattr(grace_runtime_mod, 'grace_sensitive_panel_update', fake_lease)
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)

    service = RemnaWaveService()
    service._config_error = None

    @asynccontextmanager
    async def fake_client():
        yield api

    monkeypatch.setattr(service, 'get_api_client', fake_client)

    await service.sync_users_to_panel(AsyncMock())

    assert api.update_user.await_args.kwargs['status'] is UserStatus.DISABLED


@pytest.mark.asyncio
async def test_cabinet_sync_does_not_reactivate_a_blocked_user(monkeypatch):
    updates: list[dict] = []

    class Api:
        async def get_user_by_id(self, panel_user_id):
            return SimpleNamespace(id=panel_user_id, expire_at=datetime.now(UTC) + timedelta(days=30))

        async def find_users_by_telegram_id(self, _telegram_id):
            return []

        async def find_users_by_email(self, _email):
            return []

    async def update_panel_user(_api, _sub_id, **kwargs):
        updates.append(kwargs)
        return SimpleNamespace(subscription_url='https://p/sub', happ_crypto_link='c', short_uuid='s1')

    async def create_panel_user(_api, _sub_id, **kwargs):
        raise AssertionError('аккаунт в панели есть — создавать нечего')

    class Service:
        is_configured = True

        def get_api_client(self):
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=Api())
            context.__aexit__ = AsyncMock(return_value=None)
            return context

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService', Service)
    monkeypatch.setattr('app.services.grace_access_runtime.update_panel_user_grace_safe', update_panel_user)
    monkeypatch.setattr('app.services.grace_access_runtime.create_panel_user_grace_safe', create_panel_user)
    monkeypatch.setattr('app.services.subscription_service.get_traffic_reset_strategy', lambda _tariff: 'NO_RESET')
    monkeypatch.setattr('app.utils.subscription_utils.resolve_hwid_device_limit_for_payload', lambda _sub: None)

    db = AsyncMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))

    await admin_users._sync_subscription_to_panel(db, _blocked_user(), _live_subscription())

    assert updates, 'запрос в панель обязан уйти'
    assert updates[0]['status'] is UserStatus.DISABLED
