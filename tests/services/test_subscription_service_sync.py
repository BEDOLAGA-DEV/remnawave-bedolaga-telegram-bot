"""Один помощник «создать или обновить пользователя панели» вместо 32 инлайн-копий.

Баг: бонус за регистрацию по реферальной программе заводил подписку с нуля и слал в
панель update_remnawave_user — тот требует уже известный id панели и падал с
«RemnaWave id не найден», пользователь в панели не создавался и ссылки не получал.
Правило одно на весь бот: в мультитарифе id панели живёт у подписки, вне его — у
пользователя; нет id — создаём (create — это upsert), есть — обновляем.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import Base, Subscription, SubscriptionStatus, User
from app.services.subscription_service import SubscriptionService
from tests.fixtures.sqlite_memory import memory_session


# Вся схема: get_user_by_id подтягивает промогруппы и прочие связи пользователя.
TABLES = list(Base.metadata.sorted_tables)


def _user(remnawave_id: int | None) -> User:
    return User(
        id=1,
        telegram_id=1001,
        first_name='U',
        language='ru',
        status='active',
        balance_kopeks=0,
        remnawave_id=remnawave_id,
    )


def _subscription(remnawave_id: int | None) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        remnawave_short_id='sync1',
        user_id=1,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        start_date=now,
        end_date=now + timedelta(days=3),
        traffic_limit_gb=10,
        device_limit=1,
        connected_squads=[],
        remnawave_id=remnawave_id,
    )


def _spy(monkeypatch) -> tuple[AsyncMock, AsyncMock]:
    create = AsyncMock(return_value='created')
    update = AsyncMock(return_value='updated')
    monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', create)
    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', update)
    return create, update


def _multi_tariff(monkeypatch, enabled: bool) -> None:
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', enabled)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs' if enabled else 'classic')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('multi', 'user_panel_id', 'sub_panel_id', 'expected'),
    [
        (True, None, None, 'created'),
        (True, 777, None, 'created'),  # в мультитарифе id пользователя не считается
        (True, None, 555, 'updated'),
        (False, None, None, 'created'),
        (False, 777, None, 'updated'),  # вне мультитарифа панельный аккаунт один — у пользователя
    ],
)
async def test_sync_picks_create_or_update_by_panel_id(monkeypatch, multi, user_panel_id, sub_panel_id, expected):
    _multi_tariff(monkeypatch, multi)
    create, update = _spy(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(user_panel_id))
        subscription = _subscription(sub_panel_id)
        db.add(subscription)
        await db.commit()

        result = await SubscriptionService().sync_remnawave_user(db, subscription, reset_traffic=True, reset_reason='r')

    assert result == expected
    called = create if expected == 'created' else update
    idle = update if expected == 'created' else create
    called.assert_awaited_once()
    idle.assert_not_awaited()
    assert called.await_args.kwargs == {'reset_traffic': True, 'reset_reason': 'r'}
    assert called.await_args.args[1] is subscription


@pytest.mark.asyncio
async def test_missing_user_falls_to_create_which_reports_it(monkeypatch):
    """Пользователя нет в базе: не падаем, create сам залогирует и вернёт None."""
    _multi_tariff(monkeypatch, False)
    create, update = _spy(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = _subscription(None)
        db.add(subscription)
        await db.commit()
        await SubscriptionService().sync_remnawave_user(db, subscription)
    create.assert_awaited_once()
    update.assert_not_awaited()
