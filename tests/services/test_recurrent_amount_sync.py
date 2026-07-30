"""Согласование суммы провайдерских рекуррентов с ценой продления.

Регрессия (репорт оператора): при докупке устройств/трафика на подписку с
активным рекуррентом сумма списания не менялась — провайдер продолжал брать
цену, зафиксированную при привязке, а подписка стоила уже дороже.

Изменить сумму у созданной привязки нельзя ни у Platega (в API только
create/get/cancel), ни у Lava (цена задана продуктом), поэтому устаревшая
привязка гасится, и пользователь переподключает автопродление по новой цене.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from app.database.models import (
    LavaSubscription,
    PlategaSubscription,
    PromoGroup,
    Subscription,
    SubscriptionStatus,
    Tariff,
    User,
    UserPromoGroup,
    UserStatus,
    tariff_promo_groups,
)
from app.services import recurrent_amount as ra
from app.services.recurrent_amount import (
    resolve_true_renewal_amount,
    sync_recurrent_bindings_after_price_change,
)
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
    UserPromoGroup.__table__,
    tariff_promo_groups,
    PlategaSubscription.__table__,
    LavaSubscription.__table__,
)

BASE_PRICE = 30000  # 300 ₽ за 30 дней
DEVICE_PRICE = 5000  # 50 ₽/мес за доп. устройство


async def _seed(db, *, device_limit: int = 1):
    now = datetime.now(UTC)
    user = User(
        telegram_id=808,
        username='user808',
        first_name='User',
        status=UserStatus.ACTIVE.value,
        language='ru',
        balance_kopeks=0,
    )
    db.add(user)
    await db.commit()

    tariff = Tariff(
        name='Базовый',
        is_active=True,
        device_limit=1,
        device_price_kopeks=DEVICE_PRICE,
        traffic_limit_gb=0,
        period_prices={'30': BASE_PRICE},
    )
    db.add(tariff)
    await db.commit()

    subscription = Subscription(
        user_id=user.id,
        tariff_id=tariff.id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=20),
        device_limit=device_limit,
        remnawave_short_id='shortsync',
    )
    db.add(subscription)
    await db.commit()
    return user, tariff, subscription


def _platega(subscription, user, amount: int) -> PlategaSubscription:
    return PlategaSubscription(
        user_id=user.id,
        subscription_id=subscription.id,
        tariff_id=subscription.tariff_id,
        interval=3,
        charge_days=30,
        amount_kopeks=amount,
        status='ACTIVE',
        platega_subscription_id='pl-1',
    )


def _lava(subscription, user, amount: int) -> LavaSubscription:
    return LavaSubscription(
        user_id=user.id,
        subscription_id=subscription.id,
        tariff_id=subscription.tariff_id,
        lava_product_id='prod-1',
        order_id='lavarec1_x',
        charge_days=30,
        amount_kopeks=amount,
        status='ACTIVE',
        lava_subscription_id='lv-1',
    )


async def test_true_amount_includes_extra_devices(monkeypatch):
    """Цена продления учитывает доп. устройства — это и есть «правильная» сумма."""
    async with memory_session(monkeypatch, TABLES) as db:
        _, _, subscription = await _seed(db, device_limit=3)

        amount = await resolve_true_renewal_amount(db, subscription, 30)

        # база + 2 доп. устройства × 50 ₽
        assert amount == BASE_PRICE + 2 * DEVICE_PRICE


async def test_matching_amount_keeps_bindings(monkeypatch):
    """Совпадающая сумма — привязки не трогаем."""
    async with memory_session(monkeypatch, TABLES) as db:
        user, _, subscription = await _seed(db, device_limit=1)
        db.add_all([_platega(subscription, user, BASE_PRICE), _lava(subscription, user, BASE_PRICE)])
        await db.commit()

        cancelled: list[str] = []
        import app.services.payment.lava as lava_module
        import app.services.payment.platega as platega_module

        monkeypatch.setattr(
            platega_module,
            'cancel_platega_recurring_for_subscription_safe',
            AsyncMock(side_effect=lambda *a, **k: cancelled.append('platega')),
        )
        monkeypatch.setattr(
            lava_module,
            'cancel_lava_recurring_for_subscription_safe',
            AsyncMock(side_effect=lambda *a, **k: cancelled.append('lava')),
        )

        await sync_recurrent_bindings_after_price_change(db, subscription.id)

        assert cancelled == []


async def test_stale_bindings_of_both_providers_are_cancelled(monkeypatch):
    """Подписка подорожала (докуплены устройства) — обе привязки гасятся."""
    async with memory_session(monkeypatch, TABLES) as db:
        user, _, subscription = await _seed(db, device_limit=3)
        # Привязки заведены по старой цене (до докупки устройств)
        db.add_all([_platega(subscription, user, BASE_PRICE), _lava(subscription, user, BASE_PRICE)])
        await db.commit()

        cancelled: list[str] = []
        import app.services.payment.lava as lava_module
        import app.services.payment.platega as platega_module

        async def cancel_platega(db_, sub_id, **kwargs):
            cancelled.append('platega')

        async def cancel_lava(db_, sub_id, **kwargs):
            cancelled.append('lava')

        monkeypatch.setattr(platega_module, 'cancel_platega_recurring_for_subscription_safe', cancel_platega)
        monkeypatch.setattr(lava_module, 'cancel_lava_recurring_for_subscription_safe', cancel_lava)

        await sync_recurrent_bindings_after_price_change(db, subscription.id)

        assert sorted(cancelled) == ['lava', 'platega']


async def test_sync_never_raises_on_missing_subscription(monkeypatch):
    """Best-effort: докупка уже оплачена и не должна падать из-за рекуррента."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        await sync_recurrent_bindings_after_price_change(db, 999999)  # не бросает


async def test_device_purchase_triggers_sync(monkeypatch):
    """Докупка устройств вызывает согласование сумм из CRUD."""
    async with memory_session(monkeypatch, TABLES) as db:
        _, _, subscription = await _seed(db, device_limit=1)

        called: list[int] = []

        async def fake_sync(db_, subscription_id):
            called.append(subscription_id)

        monkeypatch.setattr(ra, 'sync_recurrent_bindings_after_price_change', fake_sync)

        from app.database.crud.subscription import add_subscription_devices

        await add_subscription_devices(db, subscription, 2)

        assert called == [subscription.id]
