"""Автопродление бесплатного периода.

Владелец завёл тариф с бесплатным месяцем, человек его купил — и подписка
не продлевалась: автопродление отказывалось работать при нулевой сумме
списания и молча оставляло подписку истекать.

Проверка «за ноль не продлеваем» стояла как защита от кривой настройки: пока
бесплатный тариф завести было нельзя, нулевая цена всегда означала забытую.
Теперь эти случаи различимы — цена периода либо проставлена (пусть и нулевая),
либо нет, — и отказ остаётся только для непроставленной.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import Base, Subscription, SubscriptionStatus, Tariff, User
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)


class _FakePanelSync:
    async def update_remnawave_user(self, db, subscription, **kwargs):
        return SimpleNamespace(id=9001, used_traffic_bytes=0)

    async def create_remnawave_user(self, db, subscription, **kwargs):
        return SimpleNamespace(id=9001, used_traffic_bytes=0)


@pytest.fixture(autouse=True)
def tariffs_mode(monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'DEFAULT_AUTOPAY_PERIOD_DAYS', 0)


def _rows(period_prices: dict, *, balance_kopeks: int = 0) -> list:
    now = datetime.now(UTC)
    return [
        User(
            id=1,
            telegram_id=1001,
            first_name='U',
            language='ru',
            status='active',
            balance_kopeks=balance_kopeks,
            remnawave_id=9001,
        ),
        Tariff(
            id=1,
            name='Бесплатный месяц',
            description='',
            is_active=True,
            is_daily=False,
            period_prices=period_prices,
            traffic_limit_gb=100,
            device_limit=1,
            allowed_squads=['squad-1'],
            display_order=1,
        ),
        Subscription(
            id=10,
            remnawave_short_id='free1',
            remnawave_id=9001,
            user_id=1,
            status=SubscriptionStatus.ACTIVE.value,
            is_trial=False,
            start_date=now - timedelta(days=29),
            # Срок на исходе — попадает в окно автопродления.
            end_date=now + timedelta(hours=6),
            traffic_limit_gb=100,
            traffic_used_gb=0.0,
            device_limit=1,
            tariff_id=1,
            connected_squads=['squad-1'],
            autopay_enabled=True,
            autopay_days_before=3,
            autopay_period_days=30,
        ),
    ]


async def _run_autopay(db, monkeypatch, period_prices: dict, *, balance_kopeks: int = 0):
    import app.services.monitoring_service as monitoring_module
    import app.services.subscription_renewal_service as renewal_module

    monkeypatch.setattr(renewal_module, 'with_admin_notification_service', AsyncMock(return_value=None))

    db.add_all(_rows(period_prices, balance_kopeks=balance_kopeks))
    await db.commit()
    before = (await db.get(Subscription, 10)).end_date

    service = monitoring_module.MonitoringService.__new__(monitoring_module.MonitoringService)
    service.bot = None
    service._notified_users = set()
    service._autopay_fail_state = {}
    service.subscription_service = _FakePanelSync()

    await service._process_autopayments(db)

    subscription = await db.get(Subscription, 10)
    await db.refresh(subscription)
    return before, subscription, await db.get(User, 1)


@pytest.mark.asyncio
async def test_free_period_is_renewed(monkeypatch):
    """Бесплатный период продлевается — куплен же он был как обычный."""
    async with memory_session(monkeypatch, TABLES) as db:
        before, subscription, user = await _run_autopay(db, monkeypatch, {'30': 0})

    assert subscription.end_date > before, 'подписка не продлилась'
    assert subscription.status == SubscriptionStatus.ACTIVE.value
    assert user.balance_kopeks == 0


@pytest.mark.asyncio
async def test_unpriced_period_is_still_skipped(monkeypatch):
    """Цена периода не проставлена — продлевать нечего, поведение прежнее."""
    async with memory_session(monkeypatch, TABLES) as db:
        before, subscription, _ = await _run_autopay(db, monkeypatch, {'30': None})

    assert subscription.end_date == before


@pytest.mark.asyncio
async def test_paid_period_still_needs_money(monkeypatch):
    """Платный период без денег на балансе по-прежнему не продлевается."""
    async with memory_session(monkeypatch, TABLES) as db:
        before, subscription, user = await _run_autopay(db, monkeypatch, {'30': 20000}, balance_kopeks=0)

    assert subscription.end_date == before
    assert user.balance_kopeks == 0
