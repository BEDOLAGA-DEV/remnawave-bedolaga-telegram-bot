"""Массовая синхронизация гасит в панели дату, которая противоречит боту.

Отчёт владельца: в панели подписка активна, в боте истекла. «Из бота в панель»
переводила пользователя в DISABLED, а дата окончания в панели оставалась
прежней — будущей. Панель продолжала показывать живую подписку до этой даты, и
настоящее состояние из бота туда не попадало никогда.

Прошедшую дату ``PATCH /api/users`` не принимает («Expiration date cannot be in
the past»), поэтому гасим ближайшим допустимым моментом. После этого дата в
панели уже в прошлом, и следующие синхронизации её не трогают — та самая
«истекла минуту назад» на каждый прогон не возвращается.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.database.crud.subscription as crud_sub_mod
import app.services.grace_access_runtime as grace_runtime_mod
from app.config import Settings
from app.database.models import SubscriptionStatus
from app.services.remnawave_service import RemnaWaveService


PANEL_ID = 4242


def _subscription(*, end_date: datetime, status: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=500,
        user=SimpleNamespace(
            id=1,
            telegram_id=555,
            username='u',
            full_name='User',
            email=None,
            remnawave_id=PANEL_ID,
        ),
        user_id=1,
        status=status,
        end_date=end_date,
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


@pytest.fixture
def harness(monkeypatch):
    """Один батч из одной истёкшей подписки, панельный id уже известен."""
    subscription = _subscription(
        end_date=datetime.now(UTC) - timedelta(days=10),
        status=SubscriptionStatus.EXPIRED.value,
    )
    api = AsyncMock()
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
    return SimpleNamespace(service=service, api=api, subscription=subscription)


def _db() -> AsyncMock:
    """Сессия-двойник: единственный запрос сервиса к базе — «не держит ли этот
    панельный id другая строка подписок»."""
    db = AsyncMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    return db


def _panel_answer(expire_at: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        id=PANEL_ID,
        short_uuid='aBcD12',
        subscription_url='https://s/aBcD12',
        happ_crypto_link=None,
        expire_at=expire_at,
    )


@pytest.mark.asyncio
async def test_future_panel_date_of_an_expired_subscription_is_extinguished(harness):
    panel_says = datetime.now(UTC) + timedelta(days=300)
    harness.api.update_user.return_value = _panel_answer(panel_says)

    await harness.service.sync_users_to_panel(_db())

    calls = harness.api.update_user.await_args_list
    assert len(calls) == 2, 'после гашения панели нужен второй PATCH — он и несёт дату'

    first = calls[0].kwargs
    # Истёкшей подписке статус не уезжает: в панели нет «истекла» руками, только
    # «отключена админом» — доступ закрывает погашенная дата, истечение панель
    # выводит сама.
    assert 'status' not in first
    assert first.get('expire_at') is None, 'первым запросом дату не трогаем: вдруг там уже прошлое'

    second = calls[1].kwargs
    assert second['user_id'] == PANEL_ID
    written = second['expire_at']
    assert written < datetime.now(UTC) + timedelta(minutes=5), 'гасим ближайшим моментом, а не будущей датой'
    assert written > datetime.now(UTC), 'прошедшую дату панель не примет'


@pytest.mark.asyncio
async def test_past_panel_date_is_left_alone(harness):
    """Настоящая дата окончания в панели — история, второго запроса быть не должно."""
    harness.api.update_user.return_value = _panel_answer(datetime.now(UTC) - timedelta(days=10))

    await harness.service.sync_users_to_panel(_db())

    assert len(harness.api.update_user.await_args_list) == 1


@pytest.mark.asyncio
async def test_live_subscription_is_not_touched_twice(monkeypatch, harness):
    """У живой подписки дата уходит первым же запросом — гасить нечего."""
    harness.subscription.status = SubscriptionStatus.ACTIVE.value
    harness.subscription.end_date = datetime.now(UTC) + timedelta(days=30)
    harness.api.update_user.return_value = _panel_answer(harness.subscription.end_date)

    await harness.service.sync_users_to_panel(_db())

    calls = harness.api.update_user.await_args_list
    assert len(calls) == 1
    assert calls[0].kwargs['expire_at'] == harness.subscription.end_date
