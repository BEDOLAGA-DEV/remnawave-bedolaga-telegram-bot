"""Авто-возобновление суточной подписки после пополнения тоже обнуляет трафик.

Пятый поток суточной оплаты, который прошлый фикс не тронул. Человек пополнил
баланс — бот сразу списал за сутки и включил подписку, но в панель ушло жёсткое
«не обнулять». Тот же баг («плачу каждый день, а расход копится»), что уже
чинили в планировщике, кабинете и Mini App.

Второй хвост того же класса: этот поток берёт деньги и у подписки в статусе
«трафик исчерпан». Если списание счётчик не обнулит, брать деньги нельзя — а
если обнулит, лимит в панели надо снять явно: PATCH сам по себе его не снимает.

Проверка идёт по настоящему обработчику на реальной БД (SQLite): пользователь,
тариф, подписка, баланс и транзакция настоящие, подменена только синхронизация
с панелью — чтобы увидеть, с каким решением о сбросе бот в неё пошёл.
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

PANEL_USER_ID = 9001
DAILY_PRICE_KOPEKS = 1000
BALANCE_KOPEKS = 100_000
USED_TRAFFIC_GB = 90.0


class _FakePanelSync:
    """Подменяет SubscriptionService: запоминает, с чем к нему пришли."""

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


def _rows(*, traffic_reset_mode: str | None, status: str) -> list:
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
            is_daily_paused=False,
            start_date=now - timedelta(days=5),
            end_date=now - timedelta(hours=1),
            last_daily_charge_at=now - timedelta(days=1, hours=1),
            # Обработчик пропускает подписку, тронутую меньше минуты назад
            # (защита от гонки с планировщиком) — отодвигаем метку.
            updated_at=now - timedelta(minutes=10),
            traffic_limit_gb=100,
            traffic_used_gb=USED_TRAFFIC_GB,
            device_limit=1,
            tariff_id=1,
            connected_squads=['squad-1'],
        ),
    ]


async def _resume_after_topup(
    db,
    monkeypatch,
    *,
    traffic_reset_mode: str | None,
    reset_on_payment: bool,
    status: str = SubscriptionStatus.DISABLED.value,
):
    import app.services.subscription_auto_purchase_service as auto_purchase
    import app.services.subscription_renewal_service as renewal_module

    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', reset_on_payment)
    monkeypatch.setattr(settings, 'DEFAULT_TRAFFIC_RESET_STRATEGY', 'MONTH')

    panel = _FakePanelSync()
    monkeypatch.setattr(auto_purchase, 'SubscriptionService', lambda: panel)
    monkeypatch.setattr(renewal_module, 'with_admin_notification_service', AsyncMock(return_value=None))

    db.add_all(_rows(traffic_reset_mode=traffic_reset_mode, status=status))
    await db.commit()
    user = await db.get(User, 1)

    resumed = await auto_purchase.try_resume_disabled_daily_after_topup(db, user, bot=None)
    return panel, resumed, await db.get(Subscription, 10), await db.get(User, 1)


@pytest.mark.asyncio
async def test_topup_resume_resets_traffic_when_enabled(monkeypatch):
    """RESET_TRAFFIC_ON_PAYMENT=true — списание после пополнения обнуляет счётчик и в панели, и у себя."""
    async with memory_session(monkeypatch, TABLES) as db:
        panel, resumed, subscription, user = await _resume_after_topup(
            db, monkeypatch, traffic_reset_mode='NO_RESET', reset_on_payment=True
        )

    assert resumed is True
    assert panel.calls, 'синхронизация с панелью не выполнялась'
    assert panel.calls[0]['reset_traffic'] is True
    assert panel.calls[0]['reset_reason']
    assert subscription.traffic_used_gb == 0.0
    assert subscription.status == SubscriptionStatus.ACTIVE.value
    assert user.balance_kopeks == BALANCE_KOPEKS - DAILY_PRICE_KOPEKS


@pytest.mark.asyncio
async def test_topup_resume_keeps_traffic_when_disabled(monkeypatch):
    """Выключатель выключен — счётчик не трогаем (прежнее поведение)."""
    async with memory_session(monkeypatch, TABLES) as db:
        panel, resumed, subscription, _ = await _resume_after_topup(
            db, monkeypatch, traffic_reset_mode='NO_RESET', reset_on_payment=False
        )

    assert resumed is True
    assert panel.calls[0]['reset_traffic'] is False
    assert subscription.traffic_used_gb == USED_TRAFFIC_GB


@pytest.mark.asyncio
async def test_topup_resume_leaves_daily_reset_to_panel(monkeypatch):
    """Панель обнуляет сама раз в сутки — свой сброс не добавляем, иначе две квоты за день."""
    async with memory_session(monkeypatch, TABLES) as db:
        panel, resumed, subscription, _ = await _resume_after_topup(
            db, monkeypatch, traffic_reset_mode='DAY', reset_on_payment=True
        )

    assert resumed is True
    assert panel.calls[0]['reset_traffic'] is False
    assert subscription.traffic_used_gb == USED_TRAFFIC_GB


@pytest.mark.asyncio
async def test_topup_resume_lifts_panel_limit_after_paid_reset(monkeypatch):
    """Подписка была в лимите трафика: после оплаты со сбросом лимит в панели снимается явно."""
    async with memory_session(monkeypatch, TABLES) as db:
        panel, resumed, subscription, _ = await _resume_after_topup(
            db,
            monkeypatch,
            traffic_reset_mode='NO_RESET',
            reset_on_payment=True,
            status=SubscriptionStatus.LIMITED.value,
        )

    assert resumed is True
    assert panel.calls[0]['reset_traffic'] is True
    assert panel.enabled == [PANEL_USER_ID]
    assert subscription.status == SubscriptionStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_topup_resume_does_not_charge_limited_when_reset_impossible(monkeypatch):
    """Списание счётчик не обнулит — деньги за сутки в лимите не берём, подписку не трогаем."""
    async with memory_session(monkeypatch, TABLES) as db:
        panel, resumed, subscription, user = await _resume_after_topup(
            db,
            monkeypatch,
            traffic_reset_mode='DAY',
            reset_on_payment=True,
            status=SubscriptionStatus.LIMITED.value,
        )

    assert resumed is False
    assert panel.calls == []
    assert user.balance_kopeks == BALANCE_KOPEKS
    assert subscription.status == SubscriptionStatus.LIMITED.value
