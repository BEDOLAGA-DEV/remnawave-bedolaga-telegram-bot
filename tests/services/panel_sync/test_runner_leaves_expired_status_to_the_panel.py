"""Полный проход «в панель» не отключает истёкшие подписки.

Сценарий репорта после 4.9.0: автосинхронизация (она же кнопка «Полная
синхронизация») пошла по всем подпискам и каждой истёкшей отправила
``status=DISABLED``. В панели это «отключена администратором»: она превращала
EXPIRED в DISABLED, слала ``user.disabled``, бот отключал подписку и писал
человеку, что его отключил админ. Статус для панели решает только сборка
запроса, но именно массовый проход умножает ошибку на всю базу — поэтому
сценарий закреплён и на нём.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.database.crud.subscription as crud_sub_mod
import app.services.grace_access_runtime as grace_runtime_mod
from app.config import Settings
from app.database.models import SubscriptionStatus
from app.services.panel_sync import push_all_subscriptions


PANEL_ID = 4242


def _owner() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        telegram_id=555,
        username='u',
        full_name='User',
        email=None,
        remnawave_id=PANEL_ID,
        status='active',
    )


def _expired_subscription() -> SimpleNamespace:
    subscription = SimpleNamespace(
        id=500,
        user_id=1,
        status=SubscriptionStatus.EXPIRED.value,
        end_date=datetime.now(UTC) - timedelta(days=3),
        traffic_limit_gb=50,
        connected_squads=['squad-a'],
        tariff=None,
        remnawave_id=PANEL_ID,
        remnawave_short_uuid='aBcD12',
        remnawave_short_id='sid500',
        subscription_url='',
        subscription_crypto_link='',
        device_limit=1,
    )
    subscription.user = _owner()
    return subscription


@pytest.mark.asyncio
async def test_bulk_pass_does_not_disable_an_expired_subscription(monkeypatch):
    subscription = _expired_subscription()
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
        yield SimpleNamespace(allowed=True, subscription=subscription, has_open_grace=False, db=None)

    monkeypatch.setattr(crud_sub_mod, 'get_subscriptions_batch', fake_batch)
    monkeypatch.setattr(grace_runtime_mod, 'grace_sensitive_panel_update', fake_lease)
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)

    stats = await push_all_subscriptions(AsyncMock(), api)

    assert stats.updated == 1
    api.update_user.assert_awaited_once()
    sent = api.update_user.await_args.kwargs
    assert 'status' not in sent, f'истёкшей подписке уехал статус {sent.get("status")!r}'
    assert 'expire_at' not in sent, 'в панели дата уже прошла — гасить нечего'
