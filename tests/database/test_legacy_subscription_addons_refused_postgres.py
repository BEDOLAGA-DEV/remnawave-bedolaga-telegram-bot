"""Старой подписке докупки не продаются — на PostgreSQL, боевые обработчики.

Старая подписка — платная, без тарифа, оператор уже на тарифах. Докупка
устройств и трафика для неё считалась по классическим настройкам
(``PRICE_PER_DEVICE``, пакеты из настроек), хотя единственный путь такой
подписки — перейти на тариф. Кабинет докупки больше не показывает, а бот
отказывает до списания с понятным кодом ``tariff_required``: старый кабинет
или прямой запрос не должны продать классические опции.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.cabinet.schemas.subscription import DevicePurchaseRequest, TrafficPurchaseRequest
from app.config import settings
from app.database.models import Base, Subscription, SubscriptionStatus, User
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres

TABLES = list(Base.metadata.sorted_tables)
BALANCE_KOPEKS = 100_000
DEVICE_LIMIT = 3


@pytest.fixture(autouse=True)
def tariffs_mode_with_classic_prices(monkeypatch):
    """Режим тарифов без мультитарифа; классические цены заданы — без сторожа продажа бы состоялась."""
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'PRICE_PER_DEVICE', 5_000)
    monkeypatch.setattr(settings, 'DEVICES_SELECTION_ENABLED', True)
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 10)


async def _seed(db) -> User:
    now = datetime.now(UTC)
    user = User(telegram_id=1001, first_name='Старый', language='ru', status='active', balance_kopeks=BALANCE_KOPEKS)
    db.add(user)
    await db.flush()
    db.add(
        Subscription(
            user_id=user.id,
            status=SubscriptionStatus.ACTIVE.value,
            is_trial=False,
            tariff_id=None,
            start_date=now - timedelta(days=5),
            end_date=now + timedelta(days=20),
            traffic_limit_gb=100,
            device_limit=DEVICE_LIMIT,
            connected_squads=['squad-old'],
            remnawave_id=555,
            remnawave_short_id='legacy-short',
        )
    )
    await db.commit()
    await db.refresh(user)
    return user


def _tariff_required(exc: pytest.ExceptionInfo[HTTPException]) -> None:
    assert exc.value.status_code == 400
    assert isinstance(exc.value.detail, dict) and exc.value.detail.get('code') == 'tariff_required', exc.value.detail


async def _assert_untouched(db, user: User) -> None:
    await db.refresh(user)
    assert user.balance_kopeks == BALANCE_KOPEKS, 'деньги списывать нельзя'
    sub = (await db.execute(Subscription.__table__.select().where(Subscription.user_id == user.id))).first()
    assert sub.device_limit == DEVICE_LIMIT
    assert sub.traffic_limit_gb == 100


@pytest.mark.asyncio
async def test_device_purchase_is_refused_for_legacy_subscription(postgres_database):
    from app.cabinet.routes.subscription_modules.devices import purchase_devices

    async with postgres_session(postgres_database, TABLES) as db:
        user = await _seed(db)

        with pytest.raises(HTTPException) as exc:
            await purchase_devices(request=DevicePurchaseRequest(devices=1), subscription_id=None, user=user, db=db)

        _tariff_required(exc)
        await _assert_untouched(db, user)


@pytest.mark.asyncio
async def test_legacy_device_endpoint_is_refused_for_legacy_subscription(postgres_database):
    from app.cabinet.routes.subscription_modules.devices import purchase_devices_legacy

    async with postgres_session(postgres_database, TABLES) as db:
        user = await _seed(db)

        with pytest.raises(HTTPException) as exc:
            await purchase_devices_legacy(
                request=DevicePurchaseRequest(devices=1), subscription_id=None, user=user, db=db
            )

        _tariff_required(exc)
        await _assert_untouched(db, user)


@pytest.mark.asyncio
async def test_device_price_is_refused_for_legacy_subscription(postgres_database):
    from app.cabinet.routes.subscription_modules.devices import get_device_price

    async with postgres_session(postgres_database, TABLES) as db:
        user = await _seed(db)

        with pytest.raises(HTTPException) as exc:
            await get_device_price(devices=1, subscription_id=None, user=user, db=db)

        _tariff_required(exc)


@pytest.mark.asyncio
async def test_traffic_purchase_is_refused_for_legacy_subscription(postgres_database):
    from app.cabinet.routes.subscription_modules.traffic import purchase_traffic

    async with postgres_session(postgres_database, TABLES) as db:
        user = await _seed(db)

        with pytest.raises(HTTPException) as exc:
            await purchase_traffic(request=TrafficPurchaseRequest(gb=50), user=user, db=db, subscription_id=None)

        _tariff_required(exc)
        await _assert_untouched(db, user)


@pytest.mark.asyncio
async def test_traffic_packages_are_empty_for_legacy_subscription(postgres_database):
    from app.cabinet.routes.subscription_modules.traffic import get_traffic_packages

    async with postgres_session(postgres_database, TABLES) as db:
        user = await _seed(db)

        assert await get_traffic_packages(user=user, db=db, subscription_id=None) == []
