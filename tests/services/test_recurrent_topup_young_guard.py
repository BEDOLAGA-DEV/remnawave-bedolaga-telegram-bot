"""Layer-2 sanity guard в ``_find_subscriptions_needing_topup``.

Регрессия (кейс trial→paid дубль): юзер берёт триал (19₽), внутри 12ч
происходит дубль/конверсия — ``is_trial`` флипается в ``False`` на короткой
``end_date``, и рекуррентный autopay сразу списывает полную цену (299₽) через
привязанную карту. Защита: подписка должна «пожить» хотя бы 12ч
(``start_date <= now - 12h``) прежде чем попадёт в очередь на списание.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.database.models import (
    PromoGroup,
    Subscription,
    SubscriptionStatus,
    Tariff,
    User,
    UserPromoGroup,
    UserStatus,
    tariff_promo_groups,
)
from app.services.recurrent_payment_service import _find_subscriptions_needing_topup
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
    UserPromoGroup.__table__,
    tariff_promo_groups,
)


async def _seed_user_tariff(db) -> tuple[User, Tariff]:
    user = User(
        telegram_id=9001,
        username='guard_user',
        first_name='Guard',
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
        traffic_limit_gb=0,
        period_prices={'30': 29900},
    )
    db.add(tariff)
    await db.commit()
    return user, tariff


async def _add_sub(db, user, tariff, *, start_offset: timedelta, short_id: str) -> Subscription:
    now = datetime.now(UTC)
    sub = Subscription(
        user_id=user.id,
        tariff_id=tariff.id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        autopay_enabled=True,
        start_date=now + start_offset,
        end_date=now + timedelta(days=1),  # в горизонте autopay
        device_limit=1,
        remnawave_short_id=short_id,
    )
    db.add(sub)
    await db.commit()
    return sub


async def test_young_subscription_excluded(monkeypatch):
    """Подписка младше 12ч не попадает в очередь на рекуррентное списание."""
    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff = await _seed_user_tariff(db)
        await _add_sub(db, user, tariff, start_offset=-timedelta(hours=1), short_id='young')

        found = await _find_subscriptions_needing_topup(db)

        assert found == [], 'подписка младше 12ч должна быть исключена из autopay'


async def test_mature_subscription_included(monkeypatch):
    """Подписка старше 12ч с autopay попадает в очередь."""
    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff = await _seed_user_tariff(db)
        sub = await _add_sub(db, user, tariff, start_offset=-timedelta(hours=13), short_id='mature')

        found = await _find_subscriptions_needing_topup(db)

        assert [s.id for s in found] == [sub.id]
