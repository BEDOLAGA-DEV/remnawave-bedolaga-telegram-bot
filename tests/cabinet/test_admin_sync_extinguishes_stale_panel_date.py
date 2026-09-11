"""Синхронизация из админки гасит дату панели, если та противоречит боту.

Тот же отчёт, что и у массовой синхронизации: в панели подписка активна, в боте
истекла. Пользователь уходил в DISABLED, а дата окончания в панели оставалась
будущей — и панель до неё показывала живую подписку.

Здесь помощник уже держит панельного пользователя в руках (его тянут, чтобы
проверить, жив ли ``remnawave_id``), поэтому лишнего запроса не нужно: дата
уходит тем же PATCH.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cabinet.routes import admin_users
from app.cabinet.schemas.users import SyncToPanelRequest
from app.config import Settings


PANEL_ID = 7001


def _user() -> SimpleNamespace:
    return SimpleNamespace(
        id=10,
        full_name='Owner',
        username=None,
        telegram_id=1000,
        email=None,
        remnawave_id=PANEL_ID,
        last_remnawave_sync=None,
        updated_at=None,
        subscriptions=[],
    )


def _subscription(*, end_date: datetime, status: str = 'expired') -> SimpleNamespace:
    return SimpleNamespace(
        id=101,
        user_id=10,
        status=status,
        is_active=False,
        end_date=end_date,
        remnawave_id=None,
        remnawave_short_id=None,
        remnawave_short_uuid=None,
        traffic_limit_gb=10,
        tariff=None,
        connected_squads=[],
        device_limit=1,
        subscription_url=None,
        subscription_crypto_link=None,
    )


def _panel_double(monkeypatch, *, panel_expire_at: datetime) -> list[dict]:
    """Панель отвечает своей датой, а PATCH-и складываем для проверки."""
    updates: list[dict] = []

    class Api:
        async def get_user_by_id(self, panel_user_id):
            return SimpleNamespace(id=panel_user_id, expire_at=panel_expire_at)

        async def find_users_by_telegram_id(self, _telegram_id):
            return []

        async def find_users_by_email(self, _email):
            return []

    async def update_panel_user(_api, _sub_id, **kwargs):
        updates.append(kwargs)
        return SimpleNamespace(subscription_url='https://p/sub', happ_crypto_link='c', short_uuid='s1')

    async def create_panel_user(_api, _sub_id, **kwargs):
        raise AssertionError('пользователь в панели есть — создавать нечего')

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
    monkeypatch.setattr('app.services.panel_sync.payload.get_traffic_reset_strategy', lambda _tariff: 'NO_RESET')
    monkeypatch.setattr('app.utils.subscription_utils.resolve_hwid_device_limit_for_payload', lambda _sub: None)
    return updates


def _db() -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    return db


@pytest.mark.asyncio
async def test_future_panel_date_of_an_expired_subscription_is_extinguished(monkeypatch):
    updates = _panel_double(monkeypatch, panel_expire_at=datetime.now(UTC) + timedelta(days=300))
    subscription = _subscription(end_date=datetime.now(UTC) - timedelta(days=10))

    await admin_users._sync_subscription_to_panel(_db(), _user(), subscription)

    assert len(updates) == 1, 'дата обязана уехать тем же запросом — второй лишний'
    written = updates[0].get('expire_at')
    assert written is not None, 'панель показывает живую подписку — молчать нельзя'
    assert datetime.now(UTC) < written < datetime.now(UTC) + timedelta(minutes=5)


@pytest.mark.asyncio
async def test_past_panel_date_is_left_alone(monkeypatch):
    """В панели уже прошлое: это настоящая дата окончания, её не трогаем."""
    updates = _panel_double(monkeypatch, panel_expire_at=datetime.now(UTC) - timedelta(days=10))
    subscription = _subscription(end_date=datetime.now(UTC) - timedelta(days=10))

    await admin_users._sync_subscription_to_panel(_db(), _user(), subscription)

    assert updates and updates[0].get('expire_at') is None


@pytest.mark.asyncio
async def test_live_subscription_pushes_its_own_date(monkeypatch):
    updates = _panel_double(monkeypatch, panel_expire_at=datetime.now(UTC) - timedelta(days=10))
    end_date = datetime.now(UTC) + timedelta(days=30)
    subscription = _subscription(end_date=end_date, status='active')

    await admin_users._sync_subscription_to_panel(_db(), _user(), subscription)

    assert updates and updates[0].get('expire_at') == end_date


@pytest.mark.asyncio
async def test_sync_to_panel_endpoint_extinguishes_a_future_panel_date(monkeypatch):
    """Та же кнопка в карточке пользователя — у неё своя копия сборки запроса."""
    updates = _panel_double(monkeypatch, panel_expire_at=datetime.now(UTC) + timedelta(days=300))
    subscription = _subscription(end_date=datetime.now(UTC) - timedelta(days=10))
    user = _user()
    user.subscriptions = [subscription]
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))

    await admin_users.sync_user_to_panel(
        user.id,
        subscription_id=subscription.id,
        request=SyncToPanelRequest(),
        admin=SimpleNamespace(id=1),
        db=_db(),
    )

    assert updates, 'запрос в панель обязан уйти'
    written = updates[0].get('expire_at')
    assert written is not None, 'панель показывает живую подписку — дату надо погасить'
    assert datetime.now(UTC) < written < datetime.now(UTC) + timedelta(minutes=5)
