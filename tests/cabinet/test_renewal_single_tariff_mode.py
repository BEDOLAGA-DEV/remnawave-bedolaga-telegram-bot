"""Продление в кабинете без мультиподписок: истёкшая подписка на тарифе должна получать варианты.

Жалоба оператора (кабинет 1.73.0, один тариф на пользователя): у человека подписка истекла
27.08, тариф «Базовый», баланс есть — экран «Продлить подписку» показывает имя тарифа и
«Нет вариантов продления». У владельца (мультиподписки) всё работает.

Причина: без мультиподписок ``resolve_subscription`` берёт ``user.subscription`` из связи
``user.subscriptions`` — тариф к ней не подгружен (связь ленивая), а маршрут вариантов
обращается к ``subscription.tariff`` напрямую: в async это либо падает, либо даёт ``None`` и
уводит на «классические» периоды, у которых в режиме тарифов нет цен. Страница статуса
подгружает тариф отдельно — поэтому имя видно, а вариантов нет. В мульти-режиме подписка
грузится с ``selectinload`` — оттого у владельца работало.
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
    async def update_remnawave_user(self, db, subscription, **kwargs):
        return SimpleNamespace(id=9001, used_traffic_bytes=0)

    async def create_remnawave_user(self, db, subscription, **kwargs):
        return SimpleNamespace(id=9001, used_traffic_bytes=0)

    async def enable_remnawave_user(self, panel_user_id, db=None):
        return True


@pytest.fixture(autouse=True)
def single_tariff_mode(monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)


@pytest.fixture
def panel(monkeypatch) -> _FakePanelSync:
    import app.services.subscription_renewal_service as renewal_module
    import app.services.subscription_service as subscription_service_module

    monkeypatch.setattr(subscription_service_module, 'SubscriptionService', _FakePanelSync)
    monkeypatch.setattr(renewal_module, 'SubscriptionService', _FakePanelSync)
    return _FakePanelSync()


async def _seed_expired_paid_subscription(db) -> User:
    now = datetime.now(UTC)
    db.add_all(
        [
            User(id=1, telegram_id=1001, first_name='Даниил', language='ru', status='active', balance_kopeks=7422),
            Tariff(
                id=1,
                name='Базовый',
                description='',
                is_active=True,
                is_daily=False,
                period_prices={'30': 39900, '90': 99900},
                highlight_period_days=90,
                traffic_limit_gb=100,
                device_limit=1,
                allowed_squads=['squad-1'],
                display_order=1,
            ),
            Subscription(
                id=10,
                remnawave_short_id='base1',
                remnawave_id=9001,
                user_id=1,
                status=SubscriptionStatus.EXPIRED.value,
                is_trial=False,
                start_date=now - timedelta(days=45),
                end_date=now - timedelta(days=15),
                traffic_limit_gb=100,
                traffic_used_gb=0.0,
                device_limit=1,
                tariff_id=1,
                connected_squads=['squad-1'],
            ),
        ]
    )
    await db.commit()
    # Как в проде: пользователь приходит из зависимости маршрута свежим, без подгруженных связей.
    db.expunge_all()
    return await db.get(User, 1)


@pytest.mark.asyncio
async def test_expired_subscription_gets_tariff_periods_without_multi_tariff(monkeypatch, panel) -> None:
    from app.cabinet.routes.subscription_modules.renewal import get_renewal_options

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed_expired_paid_subscription(db)
        options = await get_renewal_options(user=user, db=db, subscription_id=10)

    assert [option.period_days for option in options] == [30, 90]
    assert [option.price_kopeks for option in options] == [39900, 99900]
    assert [option.is_highlighted for option in options] == [False, True]


@pytest.mark.asyncio
async def test_resolve_subscription_without_multi_tariff_loads_the_tariff(monkeypatch) -> None:
    """Любой маршрут, взявший подписку через resolve_subscription, может читать её тариф."""
    from sqlalchemy import inspect as sa_inspect

    from app.cabinet.routes.subscription_modules.helpers import resolve_subscription

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed_expired_paid_subscription(db)
        subscription = await resolve_subscription(db, user, None)
        assert subscription is not None and subscription.id == 10
        assert 'tariff' not in sa_inspect(subscription).unloaded, 'тариф подгружен, ленивого обращения не будет'
        assert subscription.tariff.name == 'Базовый'
