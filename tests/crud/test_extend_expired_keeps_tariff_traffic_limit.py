"""Продление истёкшей тарифной подписки обязано вернуть лимит трафика ТАРИФА.

Жалоба владельца (2026-09-13): «люди продлевают подписки, и через раз трафик
становится безлимитным, хотя у тарифов есть ограничение». Скриншот: подписка
истекла в 19:51, продлена в 19:53 — и в уведомлении «Трафик: ∞ Безлимит».

Корень: ``extend_subscription`` при включённом ``RESET_TRAFFIC_ON_PAYMENT`` для
истёкшей (или зарезанной по трафику) подписки пересобирал базовый лимит по
правилам КЛАССИЧЕСКОГО режима — из ``FIXED_TRAFFIC_LIMIT_GB`` в фиксированном
режиме выбора трафика, — хотя у подписки есть тариф со своим лимитом. С
``FIXED_TRAFFIC_LIMIT_GB=0`` это и есть «безлимит». Продление ещё живой
подписки в эту ветку не заходит — отсюда «через раз».

Проверка на настоящей БД: тариф, пользователь и подписка лежат в SQLite,
продление идёт через настоящий ``extend_subscription``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.database.crud.subscription import extend_subscription
from app.database.models import Base, Subscription, SubscriptionStatus, Tariff, TrafficPurchase, User
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)
TARIFF_LIMIT_GB = 50
TOPUP_GB = 30


def _user() -> User:
    return User(id=1, telegram_id=1001, first_name='U', language='ru', status='active', balance_kopeks=100_000)


def _tariff(*, traffic_limit_gb: int = TARIFF_LIMIT_GB, custom_traffic_enabled: bool = False) -> Tariff:
    return Tariff(
        id=1,
        name='Тариф',
        description='',
        is_active=True,
        traffic_limit_gb=traffic_limit_gb,
        custom_traffic_enabled=custom_traffic_enabled,
        device_limit=3,
        allowed_squads=['squad-1'],
        period_prices={'30': 10_000},
        display_order=1,
    )


def _subscription(
    *,
    status: str = SubscriptionStatus.EXPIRED.value,
    traffic_limit_gb: int = TARIFF_LIMIT_GB,
    purchased_traffic_gb: int = 0,
    tariff_id: int | None = 1,
    days_from_now: int = -3,
) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=10,
        remnawave_short_id='sub10',
        user_id=1,
        status=status,
        is_trial=False,
        start_date=now - timedelta(days=33),
        end_date=now + timedelta(days=days_from_now),
        traffic_limit_gb=traffic_limit_gb,
        traffic_used_gb=12.0,
        purchased_traffic_gb=purchased_traffic_gb,
        device_limit=3,
        tariff_id=tariff_id,
        connected_squads=['squad-1'],
    )


def _configure(monkeypatch, *, reset_on_payment: bool, traffic_mode: str, fixed_gb: int) -> None:
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', reset_on_payment)
    monkeypatch.setattr(settings, 'TRAFFIC_SELECTION_MODE', traffic_mode)
    monkeypatch.setattr(settings, 'FIXED_TRAFFIC_LIMIT_GB', fixed_gb)


async def _renew(monkeypatch, *rows, tariff: Tariff | None = None) -> Subscription:
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user())
        db.add(tariff or _tariff())
        for row in rows:
            db.add(row)
        await db.commit()
        subscription = await db.get(Subscription, 10)
        assert subscription is not None
        await extend_subscription(db, subscription, 30)
        await db.refresh(subscription)
        return subscription


# ── ядро жалобы: фиксированный режим с нулём даёт безлимит ──


@pytest.mark.parametrize('status', [SubscriptionStatus.EXPIRED.value, SubscriptionStatus.LIMITED.value])
async def test_expired_tariff_renewal_keeps_tariff_limit_when_fixed_limit_is_zero(monkeypatch, status):
    _configure(monkeypatch, reset_on_payment=True, traffic_mode='fixed', fixed_gb=0)

    subscription = await _renew(monkeypatch, _subscription(status=status))

    assert subscription.status == SubscriptionStatus.ACTIVE.value
    assert subscription.traffic_limit_gb == TARIFF_LIMIT_GB
    assert subscription.traffic_used_gb == 0.0


async def test_expired_tariff_renewal_ignores_fixed_limit_value(monkeypatch):
    """И ненулевая глобальная настройка тарифу не указ: у тарифа свой лимит."""
    _configure(monkeypatch, reset_on_payment=True, traffic_mode='fixed', fixed_gb=100)

    subscription = await _renew(monkeypatch, _subscription())

    assert subscription.traffic_limit_gb == TARIFF_LIMIT_GB


async def test_expired_unlimited_tariff_stays_unlimited(monkeypatch):
    _configure(monkeypatch, reset_on_payment=True, traffic_mode='fixed', fixed_gb=100)

    subscription = await _renew(monkeypatch, _subscription(traffic_limit_gb=0), tariff=_tariff(traffic_limit_gb=0))

    assert subscription.traffic_limit_gb == 0


# ── докупки поверх базы тарифа ──


async def test_expired_renewal_keeps_active_topup_on_top_of_tariff_base(monkeypatch):
    _configure(monkeypatch, reset_on_payment=True, traffic_mode='fixed', fixed_gb=0)
    now = datetime.now(UTC)

    subscription = await _renew(
        monkeypatch,
        _subscription(traffic_limit_gb=TARIFF_LIMIT_GB + TOPUP_GB, purchased_traffic_gb=TOPUP_GB),
        TrafficPurchase(subscription_id=10, traffic_gb=TOPUP_GB, expires_at=now + timedelta(days=20)),
    )

    assert subscription.traffic_limit_gb == TARIFF_LIMIT_GB + TOPUP_GB
    assert subscription.purchased_traffic_gb == TOPUP_GB


async def test_expired_renewal_drops_expired_topup_and_returns_to_tariff_base(monkeypatch):
    _configure(monkeypatch, reset_on_payment=True, traffic_mode='fixed', fixed_gb=0)
    now = datetime.now(UTC)

    subscription = await _renew(
        monkeypatch,
        _subscription(traffic_limit_gb=TARIFF_LIMIT_GB + TOPUP_GB, purchased_traffic_gb=TOPUP_GB),
        TrafficPurchase(subscription_id=10, traffic_gb=TOPUP_GB, expires_at=now - timedelta(days=5)),
    )

    assert subscription.traffic_limit_gb == TARIFF_LIMIT_GB
    assert subscription.purchased_traffic_gb == 0


# ── уже испорченная подписка выправляется при следующем продлении ──


async def test_expired_renewal_heals_zeroed_limit_in_selectable_mode(monkeypatch):
    """Подписка, которой прошлый баг уже выдал безлимит, при продлении возвращается к тарифу."""
    _configure(monkeypatch, reset_on_payment=True, traffic_mode='selectable', fixed_gb=100)

    subscription = await _renew(monkeypatch, _subscription(traffic_limit_gb=0))

    assert subscription.traffic_limit_gb == TARIFF_LIMIT_GB


# ── тариф с произвольным трафиком: база выбрана при покупке ──


async def test_custom_traffic_tariff_keeps_the_chosen_base(monkeypatch):
    _configure(monkeypatch, reset_on_payment=True, traffic_mode='fixed', fixed_gb=0)

    subscription = await _renew(
        monkeypatch,
        _subscription(traffic_limit_gb=75),
        tariff=_tariff(traffic_limit_gb=100, custom_traffic_enabled=True),
    )

    assert subscription.traffic_limit_gb == 75


async def test_custom_traffic_tariff_falls_back_to_tariff_default_when_base_is_lost(monkeypatch):
    _configure(monkeypatch, reset_on_payment=True, traffic_mode='fixed', fixed_gb=0)

    subscription = await _renew(
        monkeypatch,
        _subscription(traffic_limit_gb=0),
        tariff=_tariff(traffic_limit_gb=100, custom_traffic_enabled=True),
    )

    assert subscription.traffic_limit_gb == 100


# ── прежнее поведение, которое не должно сломаться ──


async def test_classic_expired_renewal_still_takes_the_fixed_limit(monkeypatch):
    """Без тарифа базовый лимит по-прежнему из настройки фиксированного режима."""
    _configure(monkeypatch, reset_on_payment=True, traffic_mode='fixed', fixed_gb=100)
    monkeypatch.setattr(settings, 'SALES_MODE', 'classic')

    subscription = await _renew(monkeypatch, _subscription(tariff_id=None, traffic_limit_gb=50))

    assert subscription.traffic_limit_gb == 100


async def test_active_tariff_renewal_keeps_tariff_base_plus_active_topup(monkeypatch):
    _configure(monkeypatch, reset_on_payment=True, traffic_mode='fixed', fixed_gb=0)
    now = datetime.now(UTC)

    subscription = await _renew(
        monkeypatch,
        _subscription(
            status=SubscriptionStatus.ACTIVE.value,
            days_from_now=10,
            traffic_limit_gb=TARIFF_LIMIT_GB + TOPUP_GB,
            purchased_traffic_gb=TOPUP_GB,
        ),
        TrafficPurchase(subscription_id=10, traffic_gb=TOPUP_GB, expires_at=now + timedelta(days=20)),
    )

    assert subscription.traffic_limit_gb == TARIFF_LIMIT_GB + TOPUP_GB
    assert subscription.purchased_traffic_gb == TOPUP_GB


# ── самовосстановление: испорченная подписка выправляется на ЛЮБОМ продлении ──


@pytest.mark.parametrize('reset_on_payment', [True, False])
async def test_active_tariff_renewal_heals_zeroed_limit(monkeypatch, reset_on_payment):
    """Продление ещё живой подписки тоже возвращает лимит тарифа — иначе тот, кто
    продлевает заранее, оставался бы с безлимитом навсегда."""
    _configure(monkeypatch, reset_on_payment=reset_on_payment, traffic_mode='fixed', fixed_gb=0)

    subscription = await _renew(
        monkeypatch, _subscription(status=SubscriptionStatus.ACTIVE.value, days_from_now=10, traffic_limit_gb=0)
    )

    assert subscription.traffic_limit_gb == TARIFF_LIMIT_GB


async def test_expired_renewal_with_reset_off_heals_zeroed_limit(monkeypatch):
    _configure(monkeypatch, reset_on_payment=False, traffic_mode='fixed', fixed_gb=0)

    subscription = await _renew(monkeypatch, _subscription(traffic_limit_gb=0))

    assert subscription.traffic_limit_gb == TARIFF_LIMIT_GB


async def test_active_renewal_heals_zeroed_limit_but_keeps_active_topup(monkeypatch):
    _configure(monkeypatch, reset_on_payment=True, traffic_mode='fixed', fixed_gb=0)
    now = datetime.now(UTC)

    subscription = await _renew(
        monkeypatch,
        _subscription(
            status=SubscriptionStatus.ACTIVE.value, days_from_now=10, traffic_limit_gb=0, purchased_traffic_gb=TOPUP_GB
        ),
        TrafficPurchase(subscription_id=10, traffic_gb=TOPUP_GB, expires_at=now + timedelta(days=20)),
    )

    assert subscription.traffic_limit_gb == TARIFF_LIMIT_GB + TOPUP_GB


async def test_active_renewal_of_unlimited_tariff_stays_unlimited(monkeypatch):
    _configure(monkeypatch, reset_on_payment=False, traffic_mode='fixed', fixed_gb=100)

    subscription = await _renew(
        monkeypatch,
        _subscription(status=SubscriptionStatus.ACTIVE.value, days_from_now=10, traffic_limit_gb=0),
        tariff=_tariff(traffic_limit_gb=0),
    )

    assert subscription.traffic_limit_gb == 0


async def test_classic_active_renewal_keeps_its_limit(monkeypatch):
    """Классическая подписка тарифа не имеет — её лимит продление не пересобирает."""
    _configure(monkeypatch, reset_on_payment=False, traffic_mode='selectable', fixed_gb=100)
    monkeypatch.setattr(settings, 'SALES_MODE', 'classic')

    subscription = await _renew(
        monkeypatch,
        _subscription(status=SubscriptionStatus.ACTIVE.value, days_from_now=10, tariff_id=None, traffic_limit_gb=80),
    )

    assert subscription.traffic_limit_gb == 80


async def test_reset_off_expired_renewal_keeps_the_limit(monkeypatch):
    _configure(monkeypatch, reset_on_payment=False, traffic_mode='fixed', fixed_gb=0)

    subscription = await _renew(monkeypatch, _subscription())

    assert subscription.traffic_limit_gb == TARIFF_LIMIT_GB
    assert subscription.traffic_used_gb == 12.0
