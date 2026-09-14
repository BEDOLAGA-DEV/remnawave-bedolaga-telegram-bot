"""Суточное автосписание обнуляет израсходованный трафик.

Баг владельца: у суточных тарифов счётчик трафика не сбрасывался при
автопродлении — ни в панели, ни в самом боте. Человек платил каждые сутки,
а расход копился с первой покупки, пока панель не переводила его в LIMITED.

Проверка идёт по настоящему обработчику ``_process_single_charge`` на реальной
БД: подписка, тариф, баланс и транзакция живут в SQLite, подменена только
синхронизация с панелью — чтобы увидеть, с каким решением о сбросе бот в неё
пошёл.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import Base, Subscription, SubscriptionStatus, Tariff, User
from app.services.daily_subscription_service import DailySubscriptionService
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)

DAILY_PRICE_KOPEKS = 1000


def _user() -> User:
    return User(
        id=1,
        telegram_id=1001,
        first_name='U',
        language='ru',
        status='active',
        balance_kopeks=100_000,
        remnawave_id=9001,
    )


def _tariff(traffic_reset_mode: str | None) -> Tariff:
    return Tariff(
        id=1,
        name='Суточный',
        description='',
        is_active=True,
        is_daily=True,
        daily_price_kopeks=DAILY_PRICE_KOPEKS,
        traffic_limit_gb=100,
        traffic_reset_mode=traffic_reset_mode,
        device_limit=1,
        allowed_squads=['squad-1'],
        display_order=1,
    )


def _subscription(*, status: str = SubscriptionStatus.ACTIVE.value) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=10,
        remnawave_short_id='day1',
        remnawave_id=9001,
        user_id=1,
        status=status,
        is_trial=False,
        is_daily_paused=False,
        start_date=now - timedelta(days=5),
        end_date=now + timedelta(hours=2),
        last_daily_charge_at=now - timedelta(days=1, hours=1),
        traffic_limit_gb=100,
        traffic_used_gb=90.0,
        device_limit=1,
        tariff_id=1,
        connected_squads=['squad-1'],
    )


class _FakePanelSync:
    """Подменяет SubscriptionService: запоминает, с чем к нему пришли."""

    def __init__(self) -> None:
        self.update_calls: list[dict] = []
        self.create_calls: list[dict] = []

    async def update_remnawave_user(self, db, subscription, **kwargs):
        self.update_calls.append(kwargs)
        return SimpleNamespace(id=9001, used_traffic_bytes=0)

    async def create_remnawave_user(self, db, subscription, **kwargs):
        self.create_calls.append(kwargs)
        return SimpleNamespace(id=9001, used_traffic_bytes=0)


@pytest.fixture
def panel(monkeypatch) -> _FakePanelSync:
    import app.services.subscription_renewal_service as renewal_module
    import app.services.subscription_service as subscription_service_module

    fake = _FakePanelSync()
    monkeypatch.setattr(subscription_service_module, 'SubscriptionService', lambda: fake)
    monkeypatch.setattr(renewal_module, 'with_admin_notification_service', AsyncMock(return_value=None))
    return fake


async def _charge(db, monkeypatch, *, traffic_reset_mode: str | None, reset_on_payment: bool) -> Subscription:
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', reset_on_payment)
    monkeypatch.setattr(settings, 'DEFAULT_TRAFFIC_RESET_STRATEGY', 'MONTH')

    db.add_all([_user(), _tariff(traffic_reset_mode), _subscription()])
    await db.commit()

    subscription = await DailySubscriptionService()._reload_daily_subscription(db, 10)
    service = DailySubscriptionService()
    service._bot = None
    result = await service._process_single_charge(db, subscription)
    assert result == 'charged'
    return await DailySubscriptionService()._reload_daily_subscription(db, 10)


@pytest.mark.asyncio
async def test_charge_resets_traffic_when_enabled(monkeypatch, panel):
    """RESET_TRAFFIC_ON_PAYMENT=true — списание обнуляет счётчик и в панели, и у себя."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await _charge(db, monkeypatch, traffic_reset_mode='NO_RESET', reset_on_payment=True)

    assert panel.update_calls, 'синхронизация с панелью не выполнялась'
    assert panel.update_calls[0]['reset_traffic'] is True
    assert panel.update_calls[0]['reset_reason']
    assert subscription.traffic_used_gb == 0.0


@pytest.mark.asyncio
async def test_charge_keeps_traffic_when_disabled(monkeypatch, panel):
    """Выключатель выключен — счётчик остаётся нетронутым (прежнее поведение)."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await _charge(db, monkeypatch, traffic_reset_mode='NO_RESET', reset_on_payment=False)

    assert panel.update_calls[0]['reset_traffic'] is False
    assert subscription.traffic_used_gb == 90.0


@pytest.mark.asyncio
async def test_charge_leaves_reset_to_panel_on_daily_strategy(monkeypatch, panel):
    """У тарифа суточный сброс панели — свой сброс не делаем, иначе две квоты за день."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await _charge(db, monkeypatch, traffic_reset_mode='DAY', reset_on_payment=True)

    assert panel.update_calls[0]['reset_traffic'] is False
    assert subscription.traffic_used_gb == 90.0
