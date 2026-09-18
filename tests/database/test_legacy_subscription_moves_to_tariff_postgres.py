"""Старая подписка переводится на тариф той же строкой — на PostgreSQL.

Случай владельца (2026-09-18): человек купил подписку в классике, потом
оператор включил тарифы с мультитарифом. Продлить такую подписку нельзя
(тарифа нет), а покупка тарифа с витрины заводила ВТОРУЮ подписку: старая
висела в списке навсегда — без продления, автоплатежа и смены тарифа.

Здесь кабинет присылает ``subscription_id`` старой подписки, и покупка
обязана надеть тариф на неё же: та же строка, тот же аккаунт панели (та же
ссылка у человека), остаток дней сохранён и к нему прибавлен оплаченный
период. PostgreSQL нужен из-за частичного уникального индекса «одна живая
подписка на тариф»: если такой тариф у человека уже есть, покупка отказывает
до списания денег, а не падает на индексе после.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.cabinet.schemas.subscription import TariffPurchaseRequest
from app.config import settings
from app.database.models import Base, Subscription, SubscriptionStatus, Tariff, User
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres

TABLES = list(Base.metadata.sorted_tables)
PANEL_ID = 555
PRICE_KOPEKS = 30_000
PERIOD_DAYS = 30
LEGACY_DAYS_LEFT = 10


class _FakePanelSync:
    async def update_remnawave_user(self, db, subscription, **kwargs):
        return SimpleNamespace(id=PANEL_ID, used_traffic_bytes=0)

    async def create_remnawave_user(self, db, subscription, **kwargs):
        raise AssertionError('старая подписка уже имеет аккаунт панели — создавать новый нельзя')

    async def enable_remnawave_user(self, panel_user_id, db=None):
        return True


@pytest.fixture(autouse=True)
def multi_tariff_mode(monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)


@pytest.fixture(autouse=True)
def panel(monkeypatch):
    import app.services.subscription_renewal_service as renewal_module
    import app.services.subscription_service as subscription_service_module

    monkeypatch.setattr(subscription_service_module, 'SubscriptionService', _FakePanelSync)
    monkeypatch.setattr(renewal_module, 'SubscriptionService', _FakePanelSync)


def _tariff() -> Tariff:
    return Tariff(
        name='Базовый',
        description='',
        is_active=True,
        is_daily=False,
        period_prices={str(PERIOD_DAYS): PRICE_KOPEKS},
        traffic_limit_gb=100,
        device_limit=3,
        allowed_squads=['squad-new'],
        display_order=1,
    )


def _legacy_subscription(user_id: int, now: datetime) -> Subscription:
    return Subscription(
        user_id=user_id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        tariff_id=None,
        start_date=now - timedelta(days=30),
        end_date=now + timedelta(days=LEGACY_DAYS_LEFT),
        traffic_limit_gb=0,
        device_limit=5,
        connected_squads=['squad-old'],
        remnawave_id=PANEL_ID,
        remnawave_short_id='legacy-short',
    )


async def _seed(db, now: datetime) -> tuple[User, Subscription, Tariff]:
    user = User(telegram_id=1001, first_name='Старый', language='ru', status='active', balance_kopeks=PRICE_KOPEKS)
    tariff = _tariff()
    db.add_all([user, tariff])
    await db.flush()
    legacy = _legacy_subscription(user.id, now)
    db.add(legacy)
    await db.commit()
    return user, legacy, tariff


async def _purchase(db, user: User, tariff_id: int, subscription_id: int):
    from app.cabinet.routes.subscription_modules.purchase import purchase_tariff

    await db.refresh(user)
    request = TariffPurchaseRequest(tariff_id=tariff_id, period_days=PERIOD_DAYS, subscription_id=subscription_id)
    return await purchase_tariff(request=request, user=user, db=db)


async def _user_subscriptions(db, user_id: int) -> list[Subscription]:
    # populate_existing: строки перечитываются из базы, а не из кэша сессии.
    rows = await db.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.id)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


@pytest.mark.asyncio
async def test_legacy_subscription_gets_the_tariff_in_place(postgres_database):
    """Тариф надевается на старую подписку: одна строка, тот же аккаунт панели, остаток + период."""
    now = datetime.now(UTC)
    async with postgres_session(postgres_database, TABLES) as db:
        user, legacy, tariff = await _seed(db, now)

        response = await _purchase(db, user, tariff.id, legacy.id)

        assert response.get('success') is True, response
        rows = await _user_subscriptions(db, user.id)
        assert [row.id for row in rows] == [legacy.id], 'покупка завела вторую подписку вместо перевода старой'
        moved = rows[0]
        assert moved.tariff_id == tariff.id
        assert moved.remnawave_id == PANEL_ID, 'аккаунт панели (ссылка человека) должен остаться прежним'
        assert moved.status == SubscriptionStatus.ACTIVE.value
        assert moved.is_trial is False
        expected_end = now + timedelta(days=LEGACY_DAYS_LEFT + PERIOD_DAYS)
        assert abs((moved.end_date - expected_end).total_seconds()) < 3600, (
            'остаток старой подписки должен сохраниться и к нему прибавлен оплаченный период'
        )
        await db.refresh(user)
        assert user.balance_kopeks == 0


@pytest.mark.asyncio
async def test_legacy_subscription_refuses_tariff_user_already_has(postgres_database):
    """Тариф уже есть живой подпиской — отказ до списания, старая подписка не тронута."""
    now = datetime.now(UTC)
    async with postgres_session(postgres_database, TABLES) as db:
        user, legacy, tariff = await _seed(db, now)
        already = Subscription(
            user_id=user.id,
            status=SubscriptionStatus.ACTIVE.value,
            is_trial=False,
            tariff_id=tariff.id,
            start_date=now,
            end_date=now + timedelta(days=5),
            traffic_limit_gb=100,
            device_limit=3,
            connected_squads=['squad-new'],
            remnawave_id=PANEL_ID + 1,
            remnawave_short_id='already-short',
        )
        db.add(already)
        await db.commit()

        with pytest.raises(HTTPException) as exc:
            await _purchase(db, user, tariff.id, legacy.id)

        assert exc.value.status_code == 409
        rows = {row.id: row for row in await _user_subscriptions(db, user.id)}
        assert rows[legacy.id].tariff_id is None, 'старая подписка не должна меняться при отказе'
        assert abs((rows[already.id].end_date - (now + timedelta(days=5))).total_seconds()) < 60, (
            'чужая живая подписка того же тарифа не должна продлеваться за эти деньги'
        )
        await db.refresh(user)
        assert user.balance_kopeks == PRICE_KOPEKS, 'при отказе деньги не списываются'


@pytest.mark.asyncio
async def test_legacy_subscription_without_row_panel_id_keeps_the_user_account(postgres_database):
    """Строка старой подписки без id панели, аккаунт записан у человека: перевод обновляет его, а не создаёт второй."""
    now = datetime.now(UTC)
    async with postgres_session(postgres_database, TABLES) as db:
        user = User(
            telegram_id=1001,
            first_name='Старый',
            language='ru',
            status='active',
            balance_kopeks=PRICE_KOPEKS,
            remnawave_id=PANEL_ID,
        )
        tariff = _tariff()
        db.add_all([user, tariff])
        await db.flush()
        legacy = _legacy_subscription(user.id, now)
        legacy.remnawave_id = None
        db.add(legacy)
        await db.commit()

        response = await _purchase(db, user, tariff.id, legacy.id)

        assert response.get('success') is True, response
        rows = await _user_subscriptions(db, user.id)
        assert [row.id for row in rows] == [legacy.id]
        assert rows[0].remnawave_id == PANEL_ID, (
            'аккаунт человека должен стать аккаунтом подписки, а не создаваться заново'
        )
