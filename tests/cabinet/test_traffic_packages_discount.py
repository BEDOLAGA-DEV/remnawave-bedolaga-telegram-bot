"""Regression tests for Telegram bug report #602326.

«Скидки от промогруппы не отображаются в докупке трафика» — promo-group
discounts were never shown in the cabinet's "buy traffic" sheet.

Root cause: ``GET /subscription/traffic-packages`` returned raw base prices
with no discount fields, even though ``POST /subscription/traffic`` charged the
discounted amount and the frontend ``TrafficTopupSheet`` already rendered
``discount_percent`` / ``base_price_kopeks``. The endpoint now mirrors the
device/renewal endpoints and applies the promo-group traffic discount so the
sheet can display the strike-through original price and the ``-N%`` badge.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.cabinet.routes.subscription_modules import traffic as traffic_route
from app.database.models import PromoGroup, Subscription, User


def _make_user(*, traffic_discount_percent: int, apply_to_addons: bool = True) -> User:
    pg = PromoGroup(
        name='Test promo group',
        traffic_discount_percent=traffic_discount_percent,
        server_discount_percent=0,
        device_discount_percent=0,
        apply_discounts_to_addons=apply_to_addons,
        is_default=False,
    )
    user = User(id=1, telegram_id=123, balance_kopeks=1_000_000)
    # Eager-loaded relationships (mirrors get_user_by_id): primary promo group
    # resolves via user_promo_groups first, falling back to .promo_group.
    user.promo_group = pg
    user.user_promo_groups = []
    return user


def _make_subscription() -> Subscription:
    sub = Subscription(
        id=10,
        user_id=1,
        status='active',
        is_trial=False,
        traffic_limit_gb=200,
        tariff_id=None,
    )
    sub.end_date = datetime.now(UTC) + timedelta(days=30)
    return sub


@pytest.fixture
def classic_mode(monkeypatch):
    """Drive ``get_traffic_packages`` down the classic (non-tariff) branch.

    ``settings`` is a Pydantic model, so methods are patched on the class
    (instance ``__setattr__`` is blocked for non-field names).
    """
    settings_cls = type(traffic_route.settings)
    monkeypatch.setattr(settings_cls, 'is_tariffs_mode', lambda self: False)
    monkeypatch.setattr(settings_cls, 'is_traffic_topup_enabled', lambda self: True)
    monkeypatch.setattr(
        settings_cls,
        'get_traffic_topup_packages',
        lambda self: [
            {'gb': 50, 'price': 10000, 'enabled': True},
            {'gb': 100, 'price': 18000, 'enabled': True},
        ],
    )

    async def _fake_resolve(db, user, subscription_id):
        return _make_subscription()

    monkeypatch.setattr(traffic_route, 'resolve_subscription', _fake_resolve)


@pytest.mark.asyncio
async def test_traffic_packages_expose_promo_group_discount(classic_mode):
    """A 20% traffic promo-group discount surfaces on every package."""
    user = _make_user(traffic_discount_percent=20)

    result = await traffic_route.get_traffic_packages(user=user, db=object(), subscription_id=None)

    assert len(result) == 2

    pkg50 = next(p for p in result if p.gb == 50)
    assert pkg50.discount_percent == 20
    assert pkg50.base_price_kopeks == 10000  # original (struck through in UI)
    assert pkg50.price_kopeks == 8000  # discounted price shown to the user
    assert pkg50.price_rubles == pytest.approx(80.0)

    pkg100 = next(p for p in result if p.gb == 100)
    assert pkg100.discount_percent == 20
    assert pkg100.base_price_kopeks == 18000
    assert pkg100.price_kopeks == 14400


@pytest.mark.asyncio
async def test_traffic_packages_no_discount_when_group_has_none(classic_mode):
    """No promo discount → no discount fields, raw price unchanged."""
    user = _make_user(traffic_discount_percent=0)

    result = await traffic_route.get_traffic_packages(user=user, db=object(), subscription_id=None)

    pkg50 = next(p for p in result if p.gb == 50)
    assert pkg50.discount_percent == 0
    assert pkg50.base_price_kopeks is None  # no strike-through when nothing is discounted
    assert pkg50.price_kopeks == 10000


@pytest.mark.asyncio
async def test_traffic_packages_respect_apply_discounts_to_addons_flag(classic_mode):
    """When the promo group opts out of addon discounts, traffic stays full price."""
    user = _make_user(traffic_discount_percent=30, apply_to_addons=False)

    result = await traffic_route.get_traffic_packages(user=user, db=object(), subscription_id=None)

    pkg50 = next(p for p in result if p.gb == 50)
    assert pkg50.discount_percent == 0
    assert pkg50.base_price_kopeks is None
    assert pkg50.price_kopeks == 10000


@pytest.mark.asyncio
async def test_traffic_packages_apply_discount_in_tariff_mode(monkeypatch):
    """Tariff-mode packages go through the same discount path as classic mode."""
    settings_cls = type(traffic_route.settings)
    monkeypatch.setattr(settings_cls, 'is_tariffs_mode', lambda self: True)

    class _FakeTariff:
        traffic_topup_enabled = True
        traffic_limit_gb = 200

        def get_traffic_topup_packages(self):
            return {50: 10000, 100: 18000}

    async def _fake_get_tariff(db, tariff_id):
        return _FakeTariff()

    import app.database.crud.tariff as tariff_crud

    monkeypatch.setattr(tariff_crud, 'get_tariff_by_id', _fake_get_tariff)

    sub = _make_subscription()
    sub.tariff_id = 5

    async def _fake_resolve(db, user, subscription_id):
        return sub

    monkeypatch.setattr(traffic_route, 'resolve_subscription', _fake_resolve)

    user = _make_user(traffic_discount_percent=25)

    result = await traffic_route.get_traffic_packages(user=user, db=object(), subscription_id=None)

    pkg50 = next(p for p in result if p.gb == 50)
    assert pkg50.discount_percent == 25
    assert pkg50.base_price_kopeks == 10000
    assert pkg50.price_kopeks == 7500  # 25% off


@pytest.mark.asyncio
async def test_traffic_packages_floor_displayed_price_at_one_ruble(monkeypatch):
    """An extreme discount never displays below 1₽ — matching POST's max(100,...) floor."""
    settings_cls = type(traffic_route.settings)
    monkeypatch.setattr(settings_cls, 'is_tariffs_mode', lambda self: False)
    monkeypatch.setattr(settings_cls, 'is_traffic_topup_enabled', lambda self: True)
    monkeypatch.setattr(
        settings_cls,
        'get_traffic_topup_packages',
        lambda self: [{'gb': 5, 'price': 5000, 'enabled': True}],
    )

    async def _fake_resolve(db, user, subscription_id):
        return _make_subscription()

    monkeypatch.setattr(traffic_route, 'resolve_subscription', _fake_resolve)

    user = _make_user(traffic_discount_percent=99)  # 99% of 5000 = 50 kopeks → floored to 100

    result = await traffic_route.get_traffic_packages(user=user, db=object(), subscription_id=None)

    pkg = result[0]
    assert pkg.discount_percent == 99
    assert pkg.base_price_kopeks == 5000
    assert pkg.price_kopeks == 100  # floored to 1₽, not 0.50₽
    assert pkg.discount_kopeks == 4900  # base − floored price


@pytest.mark.asyncio
async def test_traffic_packages_default_group_uses_prorated_period_hint(monkeypatch):
    """Default group period-based traffic discount uses the same ceil(remaining days)
    hint POST charges with — so the displayed percent matches the charged percent."""
    settings_cls = type(traffic_route.settings)
    monkeypatch.setattr(settings_cls, 'is_tariffs_mode', lambda self: False)
    monkeypatch.setattr(settings_cls, 'is_traffic_topup_enabled', lambda self: True)
    monkeypatch.setattr(
        settings_cls,
        'get_traffic_topup_packages',
        lambda self: [{'gb': 50, 'price': 10000, 'enabled': True}],
    )

    # Subscription with ~45 days left (minus 1h margin so ceil resolves to 45
    # regardless of test execution time). The old floor-`.days` hint resolved to
    # 44 and missed the period bucket entirely.
    sub = _make_subscription()
    sub.end_date = datetime.now(UTC) + timedelta(days=45) - timedelta(hours=1)

    async def _fake_resolve(db, user, subscription_id):
        return sub

    monkeypatch.setattr(traffic_route, 'resolve_subscription', _fake_resolve)

    pg = PromoGroup(
        name='Default',
        traffic_discount_percent=0,
        server_discount_percent=0,
        device_discount_percent=0,
        apply_discounts_to_addons=True,
        is_default=True,
        period_discounts={45: 15},
    )
    user = User(id=1, telegram_id=123, balance_kopeks=1_000_000)
    user.promo_group = pg
    user.user_promo_groups = []

    result = await traffic_route.get_traffic_packages(user=user, db=object(), subscription_id=None)

    pkg = result[0]
    assert pkg.discount_percent == 15
    assert pkg.base_price_kopeks == 10000
    assert pkg.price_kopeks == 8500  # 15% off
