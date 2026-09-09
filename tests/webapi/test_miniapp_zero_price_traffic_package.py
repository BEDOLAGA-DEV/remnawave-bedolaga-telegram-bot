"""Пакет трафика с нулевой ценой — «цена не задана», а не подарок.

Так это трактуют и бот (клавиатура докупки прямо исключает нулевые пакеты), и
кабинет (прячет из списка и отвечает «has no price configured»). Mini App был
третьей копией правила и единственной, где его не было: нулевой пакет
показывался в списке и продавался — то есть отдавал трафик бесплатно.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.config import settings
from app.database.models import Base, Subscription, SubscriptionStatus, Tariff, User
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)

PAID_GB = 50
FREE_GB = 100


@pytest.fixture(autouse=True)
def tariffs_mode(monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)


def _rows() -> list:
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
            name='С докупкой',
            description='',
            is_active=True,
            is_daily=False,
            period_prices={'30': 10000},
            traffic_limit_gb=100,
            traffic_topup_enabled=True,
            # 50 ГБ настроены, 100 ГБ — цену забыли проставить.
            traffic_topup_packages={str(PAID_GB): 5000, str(FREE_GB): 0},
            device_limit=1,
            allowed_squads=['squad-1'],
            display_order=1,
        ),
        Subscription(
            id=10,
            remnawave_short_id='pkg1',
            remnawave_id=9001,
            user_id=1,
            status=SubscriptionStatus.ACTIVE.value,
            is_trial=False,
            start_date=now - timedelta(days=1),
            end_date=now + timedelta(days=20),
            traffic_limit_gb=100,
            traffic_used_gb=0.0,
            device_limit=1,
            tariff_id=1,
            connected_squads=['squad-1'],
        ),
    ]


@pytest.mark.asyncio
async def test_zero_price_package_is_not_offered(monkeypatch):
    """Список докупки в Mini App не показывает пакет без цены."""
    from app.webapi.routes.miniapp import _get_current_tariff_model

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(_rows())
        await db.commit()
        user = await db.get(User, 1)
        subscription = await db.get(Subscription, 10)

        model = await _get_current_tariff_model(db, subscription, user)

    offered = {package.gb for package in model.traffic_topup_packages}
    assert offered == {PAID_GB}, f'нулевой пакет попал в список: {offered}'


@pytest.mark.asyncio
async def test_zero_price_package_cannot_be_bought(monkeypatch):
    """Купить пакет без цены нельзя — иначе это бесплатный трафик."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.webapi.routes import miniapp
    from app.webapi.schemas.miniapp import MiniAppTrafficTopupRequest

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(_rows())
        await db.commit()
        loaded = await db.execute(
            select(User)
            .options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
            .where(User.id == 1),
        )
        user = loaded.scalar_one()

        async def _fake_authorize(init_data, session):
            return user

        monkeypatch.setattr(miniapp, '_authorize_miniapp_user', _fake_authorize)

        with pytest.raises(HTTPException) as exc:
            await miniapp.purchase_traffic_topup_endpoint(
                payload=MiniAppTrafficTopupRequest(initData='stub', gb=FREE_GB, subscriptionId=10),
                db=db,
            )

        balance_after = (await db.get(User, 1)).balance_kopeks

    assert exc.value.status_code == 400
    assert balance_after == 100_000
