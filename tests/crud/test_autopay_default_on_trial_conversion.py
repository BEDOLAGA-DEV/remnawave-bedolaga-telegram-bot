"""``DEFAULT_AUTOPAY_ENABLED`` должен применяться при конверсии триала в платную (#3191).

Триал всегда создаётся с ``autopay_enabled=False`` — и это правильно: автоплатёж
для пробника запрещён (включение отклоняют и бот, и кабинет, выборка автоплатежей
фильтрует ``is_trial``), а экран подписки в боте показывает флаг как есть, так что
«✅ Включен» на триале был бы враньём. Настройку подписка обязана получить в момент,
когда становится платной, — иначе ``DEFAULT_AUTOPAY_ENABLED=true`` не работает
никогда, потому что первая платная подписка почти всегда вырастает из триала.

Конверсия живёт в двух местах — по одному на режим продаж:
* tariffs — ``extend_subscription`` (сброс ``is_trial`` под ``tariff_id``);
* classic — обработчики покупки, ``extend_subscription`` туда не заходит.

Здесь проверяются оба, плюс то, что продление УЖЕ платной подписки настройку не
трогает: ``is_trial = False`` в этих ветках пишется безусловно, и без гейта по
флагу конверсии продление затирало бы осознанный выбор пользователя.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.crud import subscription as sub_crud
from app.database.models import SubscriptionStatus


def _sub(**kw) -> MagicMock:
    s = MagicMock()
    s._converted_from_trial = False
    # Числовые поля должны быть числами: extend_subscription считает по ним
    # арифметику, а MagicMock падает на сравнении.
    s.purchased_traffic_gb = 0
    s.traffic_used_gb = 0.0
    s.traffic_reset_at = None
    s.autopay_days_before = 3
    s.connected_squads = []
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _db() -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()
    # execute() отдаёт синхронный Result — иначе .scalars() возвращает корутину
    result = MagicMock()
    result.scalars.return_value.first.return_value = None
    result.scalars.return_value.all.return_value = []
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.fixture(autouse=True)
def _autopay_default_on(monkeypatch):
    monkeypatch.setattr(type(sub_crud.settings), 'is_autopay_enabled_by_default', lambda self: True)
    monkeypatch.setattr(sub_crud, '_lock_subscription_row', AsyncMock())
    monkeypatch.setattr(sub_crud, '_is_free_source_tariff', AsyncMock(return_value=False))


# ============ helper ============


def test_helper_applies_the_env_default():
    subscription = _sub(autopay_enabled=False)
    sub_crud.apply_trial_conversion_defaults(subscription)
    assert subscription.autopay_enabled is True


def test_helper_respects_a_disabled_default(monkeypatch):
    monkeypatch.setattr(type(sub_crud.settings), 'is_autopay_enabled_by_default', lambda self: False)
    subscription = _sub(autopay_enabled=True)
    sub_crud.apply_trial_conversion_defaults(subscription)
    assert subscription.autopay_enabled is False


# ============ режим tariffs: extend_subscription ============


async def test_trial_conversion_enables_autopay_by_default():
    subscription = _sub(
        id=1,
        user_id=7,
        is_trial=True,
        autopay_enabled=False,
        tariff_id=3,
        status=SubscriptionStatus.TRIAL.value,
        end_date=datetime.now(UTC) + timedelta(days=3),
        traffic_limit_gb=10,
        device_limit=1,
    )

    await sub_crud.extend_subscription(_db(), subscription, 30, tariff_id=4)

    assert subscription.is_trial is False
    assert subscription.autopay_enabled is True


async def test_free_relabel_without_conversion_leaves_autopay_alone():
    """``convert_trial=False`` — бесплатная смена тарифа, триал остаётся триалом."""
    subscription = _sub(
        id=1,
        user_id=7,
        is_trial=True,
        autopay_enabled=False,
        tariff_id=3,
        status=SubscriptionStatus.TRIAL.value,
        end_date=datetime.now(UTC) + timedelta(days=3),
        traffic_limit_gb=10,
        device_limit=1,
    )

    await sub_crud.extend_subscription(_db(), subscription, 30, tariff_id=4, convert_trial=False)

    assert subscription.is_trial is True
    assert subscription.autopay_enabled is False


async def test_renewal_of_a_paid_subscription_does_not_touch_autopay():
    """Пользователь выключил автоплатёж на платной подписке — продление не включает его обратно."""
    subscription = _sub(
        id=1,
        user_id=7,
        is_trial=False,
        autopay_enabled=False,
        tariff_id=4,
        status=SubscriptionStatus.ACTIVE.value,
        end_date=datetime.now(UTC) + timedelta(days=3),
        traffic_limit_gb=100,
        device_limit=3,
    )

    await sub_crud.extend_subscription(_db(), subscription, 30, tariff_id=4)

    assert subscription.autopay_enabled is False


# ============ режим classic: обработчики покупки ============


def test_classic_purchase_paths_apply_the_default_on_conversion():
    """В classic-режиме конверсию делает не CRUD, а обработчики — они тоже обязаны применить дефолт.

    Проверка структурная: прогонять целиком оба хендлера здесь дороже, чем они
    того стоят, а забыть один из путей — ровно тот баг, который чинит #3191.
    """
    import pathlib

    for path, expected in (
        ('app/handlers/subscription/purchase.py', 2),
        ('app/services/subscription_purchase_service.py', 1),
    ):
        source = pathlib.Path(path).read_text()
        assert source.count('apply_trial_conversion_defaults(') == expected, (
            f'{path}: конверсия триала должна применять дефолт автоплатежа (ожидалось вызовов: {expected})'
        )
