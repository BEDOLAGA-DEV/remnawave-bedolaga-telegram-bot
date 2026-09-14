"""Суточная подписка, упёршаяся в лимит трафика, должна возвращаться к жизни.

Продолжение той же жалобы. Пока счётчик копился, человек рано или поздно
получал статус «трафик исчерпан» — и застревал в нём навсегда: списание берёт
только активные подписки, авто-возобновление знало про «отключена» и
«истекла», а про «исчерпан лимит» — нет. Ни одна джоба такую подписку больше
не трогала, хотя деньги на балансе есть и человек готов платить дальше.

Возврат обязан идти по суточному циклу: не раньше, чем наступили следующие
сутки, и только если это списание действительно обнулит счётчик — иначе бот
взял бы деньги и оставил человека всё с тем же исчерпанным лимитом.
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
        self.enabled: list[int] = []

    async def enable_remnawave_user(self, panel_user_id, db=None):
        self.enabled.append(panel_user_id)
        return True

    async def update_remnawave_user(self, db, subscription, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=9001, used_traffic_bytes=0)

    async def create_remnawave_user(self, db, subscription, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=9001, used_traffic_bytes=0)


def _rows(*, traffic_reset_mode: str | None, hours_since_charge: int) -> list:
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
            status=SubscriptionStatus.LIMITED.value,
            is_trial=False,
            is_daily_paused=False,
            start_date=now - timedelta(days=5),
            end_date=now + timedelta(hours=3),
            last_daily_charge_at=now - timedelta(hours=hours_since_charge),
            traffic_limit_gb=100,
            traffic_used_gb=100.0,
            device_limit=1,
            tariff_id=1,
            connected_squads=['squad-1'],
        ),
    ]


class _SessionHandle:
    """Отдаёт тестовую сессию туда, где сервис открывает свою."""

    def __init__(self, db) -> None:
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc_info) -> bool:
        return False


async def _run_auto_resume(db, monkeypatch, *, traffic_reset_mode: str | None, reset_on_payment: bool, hours: int):
    from unittest.mock import AsyncMock

    import app.services.daily_subscription_service as daily_module
    import app.services.subscription_renewal_service as renewal_module
    import app.services.subscription_service as subscription_service_module

    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', reset_on_payment)
    monkeypatch.setattr(settings, 'DEFAULT_TRAFFIC_RESET_STRATEGY', 'MONTH')

    panel = _FakePanelSync()
    monkeypatch.setattr(subscription_service_module, 'SubscriptionService', lambda: panel)
    monkeypatch.setattr(renewal_module, 'with_admin_notification_service', AsyncMock(return_value=None))
    monkeypatch.setattr(daily_module, 'AsyncSessionLocal', lambda: _SessionHandle(db))

    db.add_all(_rows(traffic_reset_mode=traffic_reset_mode, hours_since_charge=hours))
    await db.commit()

    service = daily_module.DailySubscriptionService()
    service._bot = None
    stats = await service.process_auto_resume()
    return panel, stats, await db.get(Subscription, 10), await db.get(User, 1)


@pytest.mark.asyncio
async def test_limited_subscription_is_charged_and_restored(monkeypatch):
    """Наступили следующие сутки, счётчик будет обнулён — подписка снова активна."""
    async with memory_session(monkeypatch, TABLES) as db:
        panel, stats, subscription, user = await _run_auto_resume(
            db, monkeypatch, traffic_reset_mode='NO_RESET', reset_on_payment=True, hours=25
        )

    assert stats['limit_recovered'] == 1
    assert subscription.status == SubscriptionStatus.ACTIVE.value
    assert subscription.traffic_used_gb == 0.0
    assert user.balance_kopeks == 99_000
    assert panel.calls[0]['reset_traffic'] is True
    # Панель снимает LIMITED не всегда сама — возврат просит её об этом явно.
    assert panel.enabled == [9001]


@pytest.mark.asyncio
async def test_limited_subscription_waits_for_next_day(monkeypatch):
    """Сутки ещё не прошли — не возвращаем: иначе за день можно выбрать две квоты."""
    async with memory_session(monkeypatch, TABLES) as db:
        panel, stats, subscription, user = await _run_auto_resume(
            db, monkeypatch, traffic_reset_mode='NO_RESET', reset_on_payment=True, hours=2
        )

    assert stats['limit_recovered'] == 0
    assert subscription.status == SubscriptionStatus.LIMITED.value
    assert user.balance_kopeks == 100_000
    assert panel.calls == []


@pytest.mark.asyncio
async def test_limited_subscription_untouched_when_reset_disabled(monkeypatch):
    """Обнуления не будет — брать деньги нельзя, лимит так и остался бы исчерпанным."""
    async with memory_session(monkeypatch, TABLES) as db:
        panel, stats, subscription, user = await _run_auto_resume(
            db, monkeypatch, traffic_reset_mode='NO_RESET', reset_on_payment=False, hours=25
        )

    assert stats['limit_recovered'] == 0
    assert subscription.status == SubscriptionStatus.LIMITED.value
    assert user.balance_kopeks == 100_000


@pytest.mark.asyncio
async def test_limited_subscription_left_to_panel_on_daily_strategy(monkeypatch):
    """Панель обнуляет счётчик сама раз в сутки — она же и снимет лимит."""
    async with memory_session(monkeypatch, TABLES) as db:
        panel, stats, subscription, user = await _run_auto_resume(
            db, monkeypatch, traffic_reset_mode='DAY', reset_on_payment=True, hours=25
        )

    assert stats['limit_recovered'] == 0
    assert subscription.status == SubscriptionStatus.LIMITED.value
    assert user.balance_kopeks == 100_000
