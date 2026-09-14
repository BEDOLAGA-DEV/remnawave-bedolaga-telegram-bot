"""Бесплатный тариф (0 ₽) должен покупаться и продлеваться из кабинета.

Жалоба владельца: экран покупки показывает «Период: 1 месяц — Бесплатно»,
«К оплате: Бесплатно», а нажатие «Купить» отвечает
«Invalid tariff period or pricing configuration».

Причина: кабинет считал признаком неверной настройки саму нулевую цену. Но
бесплатный тариф — штатная сущность проекта (``Tariff.is_free``, перенос дней
при смене тарифа), и бот такой тариф спокойно продаёт: он проверяет, что период
ЕСТЬ в ``period_prices``, а не что цена больше нуля. Кабинет остался строже
бота, и настроенный владельцем бесплатный тариф было невозможно купить.

Признак настроенности — наличие цены, а не её величина: непроставленная цена
(``None``) по-прежнему отклоняется.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.cabinet.schemas.subscription import TariffPurchaseRequest
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
def tariffs_mode(monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)


@pytest.fixture
def panel(monkeypatch) -> _FakePanelSync:
    import app.services.subscription_renewal_service as renewal_module
    import app.services.subscription_service as subscription_service_module

    # Именно класс, а не лямбда: модули бота вычисляют аннотацию
    # ``SubscriptionService | None`` при импорте, и функция там роняет импорт.
    #
    # Подменять надо и там, где имя УЖЕ связано импортом на уровне модуля:
    # сервис продления берёт SubscriptionService своим импортом, и патч только
    # исходного модуля работал бы или нет в зависимости от порядка импортов —
    # тест то проходил, то падал в общем прогоне.
    monkeypatch.setattr(subscription_service_module, 'SubscriptionService', _FakePanelSync)
    monkeypatch.setattr(renewal_module, 'SubscriptionService', _FakePanelSync)
    return _FakePanelSync()


def _user() -> User:
    return User(
        id=1,
        telegram_id=1001,
        first_name='U',
        language='ru',
        status='active',
        balance_kopeks=0,
    )


def _tariff(period_prices: dict) -> Tariff:
    return Tariff(
        id=1,
        name='Бесплатный',
        description='',
        is_active=True,
        is_daily=False,
        period_prices=period_prices,
        traffic_limit_gb=100,
        device_limit=1,
        allowed_squads=['squad-1'],
        display_order=1,
    )


async def _purchase(db, period_prices: dict, *, period_days: int = 30):
    from app.cabinet.routes.subscription_modules.purchase import purchase_tariff

    db.add_all([_user(), _tariff(period_prices)])
    await db.commit()
    user = await db.get(User, 1)

    request = TariffPurchaseRequest(tariff_id=1, period_days=period_days)
    return await purchase_tariff(request=request, user=user, db=db)


@pytest.mark.asyncio
async def test_free_tariff_can_be_purchased(monkeypatch, panel):
    """Настроенная нулевая цена — законная покупка, а не ошибка конфигурации."""
    async with memory_session(monkeypatch, TABLES) as db:
        response = await _purchase(db, {'30': 0})

        assert response.get('success') is True, response
        subscription = await db.get(Subscription, 1)
        assert subscription is not None, 'подписка не создана'
        assert subscription.status == SubscriptionStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_unpriced_period_is_still_rejected(monkeypatch, panel):
    """Цена периода не проставлена (None) — это как раз неверная настройка."""
    async with memory_session(monkeypatch, TABLES) as db:
        with pytest.raises(HTTPException) as exc:
            await _purchase(db, {'30': None})

        assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_unknown_period_is_still_rejected(monkeypatch, panel):
    """Период, которого у тарифа нет, отклоняется как и раньше."""
    async with memory_session(monkeypatch, TABLES) as db:
        with pytest.raises(HTTPException) as exc:
            await _purchase(db, {'30': 0}, period_days=90)

        assert exc.value.status_code == 400


# ── Продление бесплатного тарифа ──────────────────────────────────────────


def _subscription() -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=10,
        remnawave_short_id='free1',
        remnawave_id=9001,
        user_id=1,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        start_date=now - timedelta(days=5),
        end_date=now + timedelta(days=10),
        traffic_limit_gb=100,
        traffic_used_gb=0.0,
        device_limit=1,
        tariff_id=1,
        connected_squads=['squad-1'],
    )


async def _seed_free_subscription(db, period_prices: dict) -> User:
    db.add_all([_user(), _tariff(period_prices), _subscription()])
    await db.commit()
    return await db.get(User, 1)


@pytest.mark.asyncio
async def test_free_tariff_renewal_option_is_offered(monkeypatch, panel):
    """Бесплатный период должен быть в списке продления, а не пропадать из него."""
    from app.cabinet.routes.subscription_modules.renewal import get_renewal_options

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed_free_subscription(db, {'30': 0})
        options = await get_renewal_options(user=user, db=db, subscription_id=10)

    assert [option.period_days for option in options] == [30]
    assert options[0].price_kopeks == 0


@pytest.mark.asyncio
async def test_free_tariff_renewal_goes_through(monkeypatch, panel):
    """Продление бесплатного тарифа не должно падать «неверным периодом»."""
    from app.cabinet.routes.subscription_modules.renewal import renew_subscription
    from app.cabinet.schemas.subscription import RenewalRequest

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed_free_subscription(db, {'30': 0})
        before = (await db.get(Subscription, 10)).end_date

        response = await renew_subscription(
            request=RenewalRequest(period_days=30),
            user=user,
            db=db,
            subscription_id=10,
        )
        after = (await db.get(Subscription, 10)).end_date

    assert response.get('new_end_date'), response
    assert response.get('amount_paid_kopeks') == 0
    assert after > before


@pytest.mark.asyncio
async def test_unpriced_renewal_period_is_still_dropped(monkeypatch, panel):
    """Непроставленная цена периода — по-прежнему не предлагаем продление."""
    from app.cabinet.routes.subscription_modules.renewal import get_renewal_options

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed_free_subscription(db, {'30': None})
        options = await get_renewal_options(user=user, db=db, subscription_id=10)

    assert options == []
