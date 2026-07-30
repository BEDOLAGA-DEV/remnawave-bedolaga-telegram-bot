"""Превью покупки тарифа обязано считать цену тем же PricingEngine, что и confirm.

Регрессия («баланс: 100, итого: 100 → Недостаточно средств»): select_tariff_period
считал голую цену периода (_apply_promo_discount от period_prices), а
confirm_tariff_purchase — полный движок с доплатой за устройства сверх включённых
в тариф (device_limit существующей подписки). При существующей подписке того же
тарифа с бо́льшим device_limit превью показывало заниженную цену, пропускало по
балансу и рисовало «После оплаты: 0», а подтверждение отбивало покупку.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.config import settings
from app.database.models import (
    PromoGroup,
    Subscription,
    SubscriptionStatus,
    Tariff,
    User,
    UserStatus,
    tariff_promo_groups,
)
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
    tariff_promo_groups,
)

BASE_PRICE_KOPEKS = 10000  # 100 ₽ за 30 дней
DEVICE_PRICE_KOPEKS = 5000  # 50 ₽/мес за доп. устройство


def _callback(data: str):
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


def _state():
    state = MagicMock()
    state.update_data = AsyncMock()
    state.get_data = AsyncMock(return_value={})
    return state


async def test_preview_uses_engine_price_with_extra_devices(monkeypatch):
    from app.handlers.subscription import tariff_purchase as m

    async with memory_session(monkeypatch, TABLES) as db:
        monkeypatch.setattr(settings, 'PRICE_PER_DEVICE', DEVICE_PRICE_KOPEKS)

        now = datetime.now(UTC)
        user = User(
            telegram_id=111,
            username='user111',
            first_name='User',
            status=UserStatus.ACTIVE.value,
            language='ru',
            balance_kopeks=BASE_PRICE_KOPEKS,  # ровно голая цена периода
        )
        db.add(user)
        await db.commit()

        tariff = Tariff(
            name='Базовый',
            is_active=True,
            device_limit=1,
            traffic_limit_gb=0,
            period_prices={'30': BASE_PRICE_KOPEKS},
        )
        db.add(tariff)
        await db.commit()

        # Существующая подписка того же тарифа с докупленным устройством:
        # движок на confirm добавит 1 × PRICE_PER_DEVICE × 1 мес.
        db.add(
            Subscription(
                user_id=user.id,
                tariff_id=tariff.id,
                status=SubscriptionStatus.ACTIVE.value,
                is_trial=False,
                start_date=now - timedelta(days=5),
                end_date=now + timedelta(days=25),
                device_limit=2,
                remnawave_short_id='short1',
            )
        )
        await db.commit()

        monkeypatch.setattr(m.user_cart_service, 'save_user_cart', AsyncMock())

        callback = _callback(f'tariff_period:{tariff.id}:30')
        await m.select_tariff_period.__wrapped__(callback, user, db, _state())

        assert callback.message.edit_text.await_count == 1
        rendered = callback.message.edit_text.await_args.args[0]

        # Превью обязано отбить по балансу с движковой ценой (100 + 50 = 150 ₽),
        # а не показать «Подтверждение покупки» со 100 ₽ и «После оплаты: 0».
        assert 'Недостаточно средств' in rendered
        assert '150' in rendered
