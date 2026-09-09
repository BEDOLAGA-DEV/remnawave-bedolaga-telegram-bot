"""Возобновление суточной подписки тоже обнуляет трафик.

Возобновление из кабинета и из Mini App списывает суточную оплату ровно так же,
как ночной планировщик, но обе копии кода отправляли в панель жёсткое «не
обнулять». Значит, тот же баг («плачу каждый день, а расход копится») жил и на
кнопке «возобновить».
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import settings
from app.database.models import Base, Subscription, SubscriptionStatus, Tariff, User
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)


class _FakePanelSync:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def update_remnawave_user(self, db, subscription, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=9001, used_traffic_bytes=0)

    async def create_remnawave_user(self, db, subscription, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=9001, used_traffic_bytes=0)


def _rows(traffic_reset_mode: str | None) -> list:
    now = datetime.now(UTC)
    return [
        User(
            id=1,
            telegram_id=1001,
            first_name='U',
            language='ru',
            status='active',
            balance_kopeks=100_000,
            remnawave_id=9001,
        ),
        Tariff(
            id=1,
            name='Суточный',
            description='',
            is_active=True,
            is_daily=True,
            daily_price_kopeks=1000,
            traffic_limit_gb=100,
            traffic_reset_mode=traffic_reset_mode,
            device_limit=1,
            allowed_squads=['squad-1'],
            display_order=1,
        ),
        Subscription(
            id=10,
            remnawave_short_id='day1',
            remnawave_id=9001,
            user_id=1,
            status=SubscriptionStatus.DISABLED.value,
            is_trial=False,
            is_daily_paused=False,
            start_date=now - timedelta(days=5),
            end_date=now - timedelta(hours=1),
            last_daily_charge_at=now - timedelta(days=1, hours=1),
            traffic_limit_gb=100,
            traffic_used_gb=90.0,
            device_limit=1,
            tariff_id=1,
            connected_squads=['squad-1'],
        ),
    ]


async def _resume_from_cabinet(db, monkeypatch, *, traffic_reset_mode: str | None, reset_on_payment: bool):
    import app.cabinet.routes.subscription_modules.daily as cabinet_daily

    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', reset_on_payment)
    monkeypatch.setattr(settings, 'DEFAULT_TRAFFIC_RESET_STRATEGY', 'MONTH')

    panel = _FakePanelSync()
    monkeypatch.setattr(cabinet_daily, 'SubscriptionService', lambda: panel)

    db.add_all(_rows(traffic_reset_mode))
    await db.commit()
    user = await db.get(User, 1)

    await cabinet_daily.toggle_subscription_pause(user=user, db=db, subscription_id=10)
    return panel, await db.get(Subscription, 10)


@pytest.mark.asyncio
async def test_cabinet_resume_resets_traffic(monkeypatch):
    """Возобновление с оплатой обнуляет счётчик, когда выключатель включён."""
    async with memory_session(monkeypatch, TABLES) as db:
        panel, subscription = await _resume_from_cabinet(
            db, monkeypatch, traffic_reset_mode='NO_RESET', reset_on_payment=True
        )

    assert panel.calls, 'синхронизация с панелью не выполнялась'
    assert panel.calls[0]['reset_traffic'] is True
    assert subscription.traffic_used_gb == 0.0


@pytest.mark.asyncio
async def test_cabinet_resume_keeps_traffic_when_disabled(monkeypatch):
    """Выключатель выключен — счётчик не трогаем."""
    async with memory_session(monkeypatch, TABLES) as db:
        panel, subscription = await _resume_from_cabinet(
            db, monkeypatch, traffic_reset_mode='NO_RESET', reset_on_payment=False
        )

    assert panel.calls[0]['reset_traffic'] is False
    assert subscription.traffic_used_gb == 90.0


@pytest.mark.asyncio
async def test_cabinet_resume_leaves_daily_reset_to_panel(monkeypatch):
    """Панель обнуляет сама раз в сутки — свой сброс не добавляем."""
    async with memory_session(monkeypatch, TABLES) as db:
        panel, subscription = await _resume_from_cabinet(
            db, monkeypatch, traffic_reset_mode='DAY', reset_on_payment=True
        )

    assert panel.calls[0]['reset_traffic'] is False
    assert subscription.traffic_used_gb == 90.0


async def _resume_from_miniapp(db, monkeypatch, *, traffic_reset_mode: str | None, reset_on_payment: bool):
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    import app.services.subscription_service as subscription_service_module
    from app.webapi.routes import miniapp
    from app.webapi.schemas.miniapp import MiniAppDailySubscriptionToggleRequest

    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', reset_on_payment)
    monkeypatch.setattr(settings, 'DEFAULT_TRAFFIC_RESET_STRATEGY', 'MONTH')

    panel = _FakePanelSync()
    monkeypatch.setattr(subscription_service_module, 'SubscriptionService', lambda: panel)

    db.add_all(_rows(traffic_reset_mode))
    await db.commit()

    loaded = await db.execute(
        select(User).options(selectinload(User.subscriptions)).where(User.id == 1),
    )
    user = loaded.scalar_one()

    async def _fake_authorize(init_data, session):
        return user

    monkeypatch.setattr(miniapp, '_authorize_miniapp_user', _fake_authorize)

    payload = MiniAppDailySubscriptionToggleRequest(init_data='stub', subscriptionId=10)
    await miniapp.toggle_daily_subscription_pause_endpoint(payload=payload, db=db)
    return panel, await db.get(Subscription, 10)


@pytest.mark.asyncio
async def test_miniapp_resume_resets_traffic(monkeypatch):
    """Кнопка «возобновить» в Mini App живёт по той же политике, что и планировщик."""
    async with memory_session(monkeypatch, TABLES) as db:
        panel, subscription = await _resume_from_miniapp(
            db, monkeypatch, traffic_reset_mode='NO_RESET', reset_on_payment=True
        )

    assert panel.calls, 'синхронизация с панелью не выполнялась'
    assert panel.calls[0]['reset_traffic'] is True
    assert subscription.traffic_used_gb == 0.0


@pytest.mark.asyncio
async def test_miniapp_resume_keeps_traffic_when_disabled(monkeypatch):
    """Выключатель выключен — счётчик не трогаем."""
    async with memory_session(monkeypatch, TABLES) as db:
        panel, subscription = await _resume_from_miniapp(
            db, monkeypatch, traffic_reset_mode='NO_RESET', reset_on_payment=False
        )

    assert panel.calls[0]['reset_traffic'] is False
    assert subscription.traffic_used_gb == 90.0
