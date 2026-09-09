"""Отметка «выгодно» доезжает до каждой витрины периодов, а не только до покупки.

Выделение живёт у тарифа (``is_highlighted`` и ``highlight_period_days``) и до сих
пор доезжало лишь до покупки и продления подписки. Подарок и лендинг отдавали
периоды без единого признака, поэтому клиент там видел ровный список и выбирал
первый по счёту — ровно та жалоба, с которой всё началось.

Сторож требует признак от КАЖДОЙ модели периода кабинета: новая витрина (ещё один
лендинг, ещё один способ купить) не должна снова появиться слепой.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.cabinet.routes import gift as gift_routes, landing as landing_routes
from app.database.models import (
    DiscountOffer,
    GuestPurchase,
    PaymentMethodConfig,
    PromoGroup,
    PromoOfferLog,
    Subscription,
    SystemSetting,
    Tariff,
    Transaction,
    User,
    UserPromoGroup,
    Webhook,
    tariff_promo_groups,
)
from app.services.gift_purchase_service import GIFT_ENABLED_KEY
from tests.fixtures.sqlite_memory import memory_session


_TABLES = [
    SystemSetting.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
    tariff_promo_groups,
    UserPromoGroup.__table__,
    Subscription.__table__,
    User.__table__,
    GuestPurchase.__table__,
    Transaction.__table__,
    DiscountOffer.__table__,
    PromoOfferLog.__table__,
    PaymentMethodConfig.__table__,
    Webhook.__table__,
]


def _tariff(**overrides) -> Tariff:
    """Тариф с тремя периодами; выгодным по умолчанию отмечен последний."""
    fields = {
        'id': 1,
        'name': 'Базовый',
        'is_active': True,
        'show_in_gift': True,
        'display_order': 1,
        'period_prices': {'30': 39900, '180': 156000, '360': 264000},
        'highlight_period_days': 360,
        'is_highlighted': True,
        'device_limit': 1,
        'traffic_limit_gb': 50,
    }
    fields.update(overrides)
    return Tariff(**fields)


# ── Подарок ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gift_config_marks_the_highlighted_period(monkeypatch):
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        db.add(User(id=10, balance_kopeks=0, username='buyer'))
        db.add(_tariff())
        await db.commit()

        config = await gift_routes.get_gift_config(user=await db.get(User, 10), db=db)

        marked = [p.days for p in config.tariffs[0].periods if p.is_highlighted]
        assert marked == [360]


@pytest.mark.asyncio
async def test_gift_config_marks_the_highlighted_tariff(monkeypatch):
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        db.add(User(id=10, balance_kopeks=0, username='buyer'))
        db.add(_tariff(id=1, display_order=1, is_highlighted=False))
        db.add(_tariff(id=2, name='Годовой', display_order=2, is_highlighted=True))
        await db.commit()

        config = await gift_routes.get_gift_config(user=await db.get(User, 10), db=db)

        assert [t.id for t in config.tariffs if t.is_highlighted] == [2]


@pytest.mark.asyncio
async def test_gift_config_marks_nothing_without_a_highlight(monkeypatch):
    """Без отметки оператора витрина остаётся ровной — как была."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        db.add(User(id=10, balance_kopeks=0, username='buyer'))
        db.add(_tariff(highlight_period_days=None, is_highlighted=False))
        await db.commit()

        config = await gift_routes.get_gift_config(user=await db.get(User, 10), db=db)

        assert not any(p.is_highlighted for p in config.tariffs[0].periods)
        assert not any(t.is_highlighted for t in config.tariffs)


# ── Лендинг ─────────────────────────────────────────────────────────────────


def _landing(**overrides) -> SimpleNamespace:
    """Минимальный дубль страницы лендинга: загрузчику тарифов нужны два поля."""
    fields = {'allowed_tariff_ids': [1], 'allowed_periods': None}
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.mark.asyncio
async def test_landing_marks_the_highlighted_period_and_tariff(monkeypatch):
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(_tariff())
        await db.commit()

        tariffs = await landing_routes._load_landing_tariffs(db, _landing())

        assert [p.days for p in tariffs[0].periods if p.is_highlighted] == [360]
        assert tariffs[0].is_highlighted is True


@pytest.mark.asyncio
async def test_landing_marks_nothing_when_the_highlighted_period_is_not_offered(monkeypatch):
    """Лендинг вправе сузить набор периодов: отметка на выброшенном периоде
    не должна перескакивать на соседний."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(_tariff())
        await db.commit()

        tariffs = await landing_routes._load_landing_tariffs(db, _landing(allowed_periods={'1': [30, 180]}))

        assert [p.days for p in tariffs[0].periods] == [30, 180]
        assert not any(p.is_highlighted for p in tariffs[0].periods)


# ── Сторож ──────────────────────────────────────────────────────────────────


# Периоды, которые видит только оператор: в админке выделение живёт отдельным
# полем тарифа (``highlight_period_days``), а период там не выбирают, а правят.
_ADMIN_ONLY_PERIOD_MODELS = frozenset(
    {
        'app.cabinet.schemas.tariffs.PeriodPrice',
        'app.cabinet.schemas.users.PeriodPriceInfo',
    }
)


def _period_models() -> dict[str, set[str]]:
    """Все модели кабинета, описывающие период с ценой."""
    from app.cabinet import routes as routes_pkg, schemas as schemas_pkg

    found: dict[str, set[str]] = {}
    for package in (routes_pkg, schemas_pkg):
        for module_info in pkgutil.walk_packages(package.__path__, package.__name__ + '.'):
            module = importlib.import_module(module_info.name)
            for obj in vars(module).values():
                if not isinstance(obj, type) or not issubclass(obj, BaseModel) or obj is BaseModel:
                    continue
                fields = set(obj.model_fields)
                if 'price_kopeks' in fields and fields & {'days', 'period_days'}:
                    found[f'{obj.__module__}.{obj.__name__}'] = fields
    return found


def test_every_client_period_model_carries_the_best_value_flag():
    blind = sorted(
        name
        for name, fields in _period_models().items()
        if name not in _ADMIN_ONLY_PERIOD_MODELS and 'is_highlighted' not in fields
    )

    assert not blind, (
        'Витрина периодов без признака выгодного периода — клиент снова увидит '
        f'ровный список и выберет первый по счёту: {blind}'
    )


def test_admin_only_exceptions_still_exist():
    """Список исключений не должен гнить: исчезнувшая модель прячет новую слепую."""
    known = set(_period_models())

    assert known >= _ADMIN_ONLY_PERIOD_MODELS, sorted(_ADMIN_ONLY_PERIOD_MODELS - known)


def test_the_guard_actually_sees_the_showcases():
    """Сторож бесполезен, если ничего не находит: три известные витрины обязаны
    попадать в выборку. Покупка тарифа отдаёт периоды словарём, а не моделью, —
    её признак закреплён отдельными тестами ручки."""
    client_models = set(_period_models()) - _ADMIN_ONLY_PERIOD_MODELS

    assert client_models >= {
        'app.cabinet.routes.landing.LandingTariffPeriod',
        'app.cabinet.schemas.gift.GiftConfigTariffPeriod',
        'app.cabinet.schemas.subscription.RenewalOptionResponse',
    }, sorted(client_models)
