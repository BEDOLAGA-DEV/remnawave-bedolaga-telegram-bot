"""Кнопка «возобновить» суточной подписки в самом боте тоже обнуляет трафик.

Четвёртый поток суточной оплаты, который прошлый фикс не тронул: кабинет и Mini
App починили, а у той же кнопки в Telegram-меню осталось жёсткое «не обнулять».
Человек нажимал «возобновить», платил за сутки, а израсходованный трафик
продолжал копиться с первой покупки.

Проверка идёт по настоящему обработчику на реальной БД (SQLite): пользователь,
тариф, подписка, баланс и транзакция настоящие, подменена только синхронизация
с панелью и отрисовка экрана после нажатия.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import Base, Subscription, SubscriptionStatus, Tariff, User
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)

PANEL_USER_ID = 9001
DAILY_PRICE_KOPEKS = 1000
BALANCE_KOPEKS = 100_000
USED_TRAFFIC_GB = 90.0


class _FakePanelSync:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.enabled: list[int] = []

    async def update_remnawave_user(self, db, subscription, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=PANEL_USER_ID, used_traffic_bytes=0)

    async def create_remnawave_user(self, db, subscription, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=PANEL_USER_ID, used_traffic_bytes=0)

    async def enable_remnawave_user(self, panel_user_id, db=None):
        self.enabled.append(panel_user_id)
        return True


def _rows(*, traffic_reset_mode: str | None, status: str, paused: bool = False) -> list:
    now = datetime.now(UTC)
    return [
        User(
            id=1,
            telegram_id=1001,
            first_name='U',
            language='ru',
            status='active',
            balance_kopeks=BALANCE_KOPEKS,
            remnawave_id=PANEL_USER_ID,
        ),
        Tariff(
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
        ),
        Subscription(
            id=10,
            remnawave_short_id='day1',
            remnawave_id=PANEL_USER_ID,
            user_id=1,
            status=status,
            is_trial=False,
            is_daily_paused=paused,
            start_date=now - timedelta(days=5),
            end_date=now - timedelta(hours=1),
            last_daily_charge_at=now - timedelta(days=1, hours=1),
            traffic_limit_gb=100,
            traffic_used_gb=USED_TRAFFIC_GB,
            device_limit=1,
            tariff_id=1,
            connected_squads=['squad-1'],
        ),
    ]


async def _resume_from_bot(
    db,
    monkeypatch,
    *,
    traffic_reset_mode: str | None,
    reset_on_payment: bool,
    status: str = SubscriptionStatus.DISABLED.value,
    paused: bool = False,
):
    import app.handlers.subscription.purchase as purchase_module
    import app.services.subscription_service as subscription_service_module

    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', reset_on_payment)
    monkeypatch.setattr(settings, 'DEFAULT_TRAFFIC_RESET_STRATEGY', 'MONTH')

    panel = _FakePanelSync()
    monkeypatch.setattr(subscription_service_module, 'SubscriptionService', lambda: panel)
    # После нажатия обработчик перерисовывает экран подписки — это не предмет проверки.
    monkeypatch.setattr(purchase_module, 'show_subscription_info', AsyncMock(return_value=None))

    db.add_all(_rows(traffic_reset_mode=traffic_reset_mode, status=status, paused=paused))
    await db.commit()

    loaded = await db.execute(
        select(User).options(selectinload(User.subscriptions).selectinload(Subscription.tariff)).where(User.id == 1),
    )
    user = loaded.scalar_one()

    callback = SimpleNamespace(
        answer=AsyncMock(),
        bot=None,
        message=SimpleNamespace(edit_text=AsyncMock()),
        from_user=SimpleNamespace(id=1001),
    )

    await purchase_module.handle_toggle_daily_subscription_pause(callback, user, db)
    return panel, callback, await db.get(Subscription, 10), await db.get(User, 1)


@pytest.mark.asyncio
async def test_bot_resume_resets_traffic_when_enabled(monkeypatch):
    """RESET_TRAFFIC_ON_PAYMENT=true — оплата возобновления обнуляет счётчик и в панели, и у себя."""
    async with memory_session(monkeypatch, TABLES) as db:
        panel, callback, subscription, user = await _resume_from_bot(
            db, monkeypatch, traffic_reset_mode='NO_RESET', reset_on_payment=True
        )

    assert panel.calls, 'синхронизация с панелью не выполнялась'
    assert panel.calls[0]['reset_traffic'] is True
    assert panel.calls[0]['reset_reason']
    assert subscription.traffic_used_gb == 0.0
    assert subscription.status == SubscriptionStatus.ACTIVE.value
    assert user.balance_kopeks == BALANCE_KOPEKS - DAILY_PRICE_KOPEKS
    callback.answer.assert_awaited()


@pytest.mark.asyncio
async def test_bot_resume_keeps_traffic_when_disabled(monkeypatch):
    """Выключатель выключен — счётчик не трогаем (прежнее поведение)."""
    async with memory_session(monkeypatch, TABLES) as db:
        panel, _, subscription, _ = await _resume_from_bot(
            db, monkeypatch, traffic_reset_mode='NO_RESET', reset_on_payment=False
        )

    assert panel.calls[0]['reset_traffic'] is False
    assert subscription.traffic_used_gb == USED_TRAFFIC_GB


@pytest.mark.asyncio
async def test_bot_resume_leaves_daily_reset_to_panel(monkeypatch):
    """Панель обнуляет сама раз в сутки — свой сброс не добавляем, иначе две квоты за день."""
    async with memory_session(monkeypatch, TABLES) as db:
        panel, _, subscription, _ = await _resume_from_bot(
            db, monkeypatch, traffic_reset_mode='DAY', reset_on_payment=True
        )

    assert panel.calls[0]['reset_traffic'] is False
    assert subscription.traffic_used_gb == USED_TRAFFIC_GB


@pytest.mark.asyncio
async def test_bot_resume_lifts_panel_limit_after_paid_reset(monkeypatch):
    """Подписка была в лимите трафика: после оплаты со сбросом лимит в панели снимается явно."""
    async with memory_session(monkeypatch, TABLES) as db:
        panel, _, subscription, _ = await _resume_from_bot(
            db,
            monkeypatch,
            traffic_reset_mode='NO_RESET',
            reset_on_payment=True,
            status=SubscriptionStatus.LIMITED.value,
        )

    assert panel.calls[0]['reset_traffic'] is True
    assert panel.enabled == [PANEL_USER_ID]
    assert subscription.status == SubscriptionStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_bot_unpause_without_charge_keeps_traffic(monkeypatch):
    """Снятие своей паузы у активной подписки — не оплата: денег не берём и счётчик не трогаем."""
    async with memory_session(monkeypatch, TABLES) as db:
        panel, _, subscription, user = await _resume_from_bot(
            db,
            monkeypatch,
            traffic_reset_mode='NO_RESET',
            reset_on_payment=True,
            status=SubscriptionStatus.ACTIVE.value,
            paused=True,
        )

    assert panel.calls, 'синхронизация с панелью не выполнялась'
    assert panel.calls[0]['reset_traffic'] is False
    assert panel.enabled == []
    assert subscription.traffic_used_gb == USED_TRAFFIC_GB
    assert subscription.is_daily_paused is False
    assert user.balance_kopeks == BALANCE_KOPEKS
