"""Продление не роняет лимит истёкшей докупки раньше сброса used панелью.

Ежедневная уборка докупок давно откладывает понижение лимита, если оно загонит
человека за лимит, а панель сама обнулит used (``_should_defer_limit_drop``).
Продление этого правила не знало: в окне «докупка истекла — сброс ещё не
наступил» ``extend_subscription`` удалял пакет и ставил базу тарифа, used
оставался прежним, и панель ставила LIMITED через секунды после оплаты.

Случай с прода: тариф 100 ГБ, MONTH; докупка 1000 ГБ истекла накануне; used
188 ГБ; автопродление — человек заплатил и сразу «достигнут лимит».
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.crud.subscription import extend_subscription, reconcile_tariff_traffic_limit
from app.database.models import Base, Subscription, SubscriptionStatus, Tariff, TrafficPurchase, User
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)


@pytest.fixture(autouse=True)
def _mode(monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)

    async def panel_usage(db, subscription):
        return subscription.traffic_used_gb

    monkeypatch.setattr('app.services.traffic_package_usage.read_panel_used_gb', panel_usage)


async def _seed(db, *, used_gb: float, reset_mode: str = 'MONTH', expired_days_ago: float = 1) -> Subscription:
    now = datetime.now(UTC)
    db.add(User(id=1, telegram_id=1001, first_name='U', language='ru', status='active', balance_kopeks=0))
    db.add(
        Tariff(
            id=1,
            name='Start',
            description='',
            is_active=True,
            traffic_limit_gb=100,
            traffic_reset_mode=reset_mode,
            device_limit=2,
            period_prices={'30': 19_900},
            display_order=1,
        )
    )
    subscription = Subscription(
        id=10,
        user_id=1,
        remnawave_short_id='sub10',
        remnawave_id=163,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        start_date=now - timedelta(days=60),
        end_date=now + timedelta(hours=2),
        traffic_limit_gb=1100,
        purchased_traffic_gb=1000,
        traffic_used_gb=used_gb,
        traffic_reset_at=now - timedelta(days=expired_days_ago),
        device_limit=3,
        tariff_id=1,
    )
    db.add(subscription)
    db.add(TrafficPurchase(subscription_id=10, traffic_gb=1000, expires_at=now - timedelta(days=expired_days_ago)))
    await db.commit()
    return subscription


async def _purchases(db) -> list[int]:
    return [p.traffic_gb for p in (await db.scalars(select(TrafficPurchase))).all()]


@pytest.mark.asyncio
@pytest.mark.parametrize('strategy', ['DAY', 'WEEK', 'MONTH', 'MONTH_ROLLING'])
async def test_renewal_keeps_expired_topup_while_used_is_above_the_tariff_base(monkeypatch, strategy) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await _seed(db, used_gb=188, reset_mode=strategy)

        await extend_subscription(db, subscription, 30)
        await db.refresh(subscription)
        purchases = await _purchases(db)

    assert subscription.traffic_limit_gb == 1100, 'продление отрезало оплатившего: лимит ниже расхода'
    assert subscription.purchased_traffic_gb == 1000
    assert purchases == [1000], 'пакет уберёт ежедневная уборка после сброса used панелью'


@pytest.mark.asyncio
async def test_recurring_charge_entry_keeps_expired_topup_too(monkeypatch) -> None:
    """Рекуррентные Lava/Platega продлевают мимо CRUD и зовут reconcile_tariff_traffic_limit."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await _seed(db, used_gb=188)

        subscription.extend_subscription(30)
        await reconcile_tariff_traffic_limit(db, subscription)
        await db.commit()
        await db.refresh(subscription)

    assert subscription.traffic_limit_gb == 1100


@pytest.mark.asyncio
async def test_renewal_drops_expired_topup_when_used_fits_the_tariff_base(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await _seed(db, used_gb=40)

        await extend_subscription(db, subscription, 30)
        await db.refresh(subscription)
        purchases = await _purchases(db)

    assert subscription.traffic_limit_gb == 100
    assert subscription.purchased_traffic_gb == 0
    assert purchases == []


@pytest.mark.asyncio
async def test_renewal_drops_expired_topup_without_periodic_panel_reset(monkeypatch) -> None:
    """NO_RESET: панель used не обнулит — ждать нечего, понижаем как раньше."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await _seed(db, used_gb=188, reset_mode='NO_RESET')

        await extend_subscription(db, subscription, 30)
        await db.refresh(subscription)

    assert subscription.traffic_limit_gb == 100


@pytest.mark.asyncio
async def test_renewal_stops_waiting_after_forty_days(monkeypatch) -> None:
    """Защита от вечного откладывания — тот же порог, что у ежедневной уборки."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await _seed(db, used_gb=188, expired_days_ago=41)

        await extend_subscription(db, subscription, 30)
        await db.refresh(subscription)

    assert subscription.traffic_limit_gb == 100


@pytest.mark.asyncio
@pytest.mark.parametrize('expired', [False, True])
async def test_review_explicit_same_tariff_renewal_preserves_deferred_package(monkeypatch, expired):
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await _seed(db, used_gb=188)
        if expired:
            subscription.end_date = datetime.now(UTC) - timedelta(hours=1)
            await db.commit()
        await extend_subscription(db, subscription, 30, tariff_id=1, traffic_limit_gb=100, reset_used_traffic=False)
        await db.refresh(subscription)
        assert subscription.traffic_used_gb == 188
        assert subscription.traffic_limit_gb == 1100
        assert await _purchases(db) == [1000]


@pytest.mark.asyncio
async def test_review_daily_deferral_survives_renewal_with_stale_local_usage(monkeypatch):
    from unittest.mock import AsyncMock

    from app.services.daily_subscription_service import DailySubscriptionService

    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await _seed(db, used_gb=40)
        subscription.tariff = await db.get(Tariff, 1)
        packages = list((await db.scalars(select(TrafficPurchase))).all())
        service = DailySubscriptionService()
        monkeypatch.setattr(service, '_get_panel_used_gb', AsyncMock(return_value=188))
        assert await service._should_defer_limit_drop(db, subscription, 100, packages)
        assert subscription.traffic_used_gb == 40
        monkeypatch.setattr('app.services.traffic_package_usage.read_panel_used_gb', AsyncMock(return_value=188))
        await extend_subscription(db, subscription, 30)
        await db.refresh(subscription)
        assert subscription.traffic_limit_gb == 1100


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit', [False, True])
async def test_panel_unavailable_retains_package_until_worker_can_check(monkeypatch, explicit):
    from unittest.mock import AsyncMock

    monkeypatch.setattr('app.services.traffic_package_usage.read_panel_used_gb', AsyncMock(return_value=None))
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await _seed(db, used_gb=0)
        kwargs = dict(traffic_limit_gb=100, reset_used_traffic=False) if explicit else {}
        await extend_subscription(db, subscription, 30, **kwargs)
        assert subscription.traffic_limit_gb == 1100
        assert await _purchases(db) == [1000]


@pytest.mark.asyncio
async def test_confirmed_panel_reset_removes_expired_only_despite_stale_high_cache(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr('app.services.traffic_package_usage.read_panel_used_gb', AsyncMock(return_value=0))
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await _seed(db, used_gb=188)
        subscription.purchased_traffic_gb = 1050
        subscription.traffic_limit_gb = 1150
        db.add(TrafficPurchase(subscription_id=10, traffic_gb=50, expires_at=datetime.now(UTC) + timedelta(days=10)))
        await db.commit()
        await extend_subscription(db, subscription, 30)
        assert subscription.traffic_limit_gb == 150
        assert subscription.purchased_traffic_gb == 50
        assert await _purchases(db) == [50]


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['payment_reset', 'explicit_reset', 'switch', 'unlimited'])
async def test_reset_switch_and_unlimited_do_not_defer(monkeypatch, case):
    from unittest.mock import AsyncMock

    reader = AsyncMock(return_value=188)
    monkeypatch.setattr('app.services.traffic_package_usage.read_panel_used_gb', reader)
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await _seed(db, used_gb=188)
        kwargs = {}
        expected = 100
        if case == 'payment_reset':
            monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', True)
        elif case == 'explicit_reset':
            kwargs = dict(traffic_limit_gb=100, reset_used_traffic=True)
        elif case == 'switch':
            db.add(Tariff(id=2, name='Other', traffic_limit_gb=200, device_limit=2, period_prices={'30': 10000}))
            await db.commit()
            kwargs = dict(tariff_id=2, traffic_limit_gb=200, reset_used_traffic=False)
            expected = 200
        else:
            tariff = await db.get(Tariff, 1)
            tariff.traffic_limit_gb = 0
            await db.commit()
            expected = 0
        await extend_subscription(db, subscription, 30, **kwargs)
        assert subscription.traffic_limit_gb == expected
        assert await _purchases(db) == []
        reader.assert_not_awaited()
