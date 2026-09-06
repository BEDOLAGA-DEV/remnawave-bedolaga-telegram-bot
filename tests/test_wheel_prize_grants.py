"""Regression tests for how the Fortune Wheel actually hands over a prize.

B1 — get_wheel_prizes must order deterministically: _calculate_rotation maps a
     prize to a sector by its index in that list, so equal sort_order values
     (the default!) let the DB return an arbitrary order and the pointer stops
     on a sector that is not the announced prize.
B2 — a promocode prize must carry every configured bonus into the generated
     code: promo_traffic_gb was declared on WheelPrize and settable in the admin
     UI, but never written to the PromoCode, and the traffic leg only applies
     for type BALANCE_AND_DAYS.
B3 — a traffic prize must go through add_subscription_traffic (which records a
     TrafficPurchase row), not a raw `traffic_limit_gb +=`: the limit is
     recomputed as base_limit + purchased_gb, so a raw bump is wiped by the
     next renewal or top-up.
B4 — the promocode win message must say what is inside the code and how long it
     is valid.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.database.models import PromoCodeType, WheelPrizeType
from app.services.wheel_service import FortuneWheelService


def _prize(**kwargs) -> SimpleNamespace:
    base = dict(
        id=1,
        prize_type=WheelPrizeType.PROMOCODE.value,
        prize_value=0,
        prize_value_kopeks=0,
        display_name='Промокод',
        promo_balance_bonus_kopeks=0,
        promo_subscription_days=0,
        promo_traffic_gb=0,
        promo_group_id=None,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_get_wheel_prizes_orders_by_sort_order_then_id() -> None:
    """B1: без вторичного ключа порядок при равных sort_order произволен."""
    import inspect

    from app.database.crud import wheel as wheel_crud

    source = inspect.getsource(wheel_crud.get_wheel_prizes)
    assert 'WheelPrize.sort_order, WheelPrize.id' in source


@pytest.mark.asyncio
async def test_promocode_prize_carries_traffic_and_promo_group() -> None:
    """B2: гигабайты и промогруппа обязаны доехать до промокода."""
    svc = FortuneWheelService()
    prize = _prize(promo_subscription_days=7, promo_traffic_gb=50, promo_group_id=3)
    config = SimpleNamespace(promo_prefix='WHEEL', promo_validity_days=30)
    db = SimpleNamespace(add=lambda obj: None, flush=AsyncMock())

    created = await svc._generate_prize_promocode(db, SimpleNamespace(id=42), prize, config)

    assert created.traffic_gb == 50
    assert created.promo_group_id == 3
    assert created.subscription_days == 7
    # Трафик применяется только у BALANCE_AND_DAYS (promocode_service).
    assert created.type == PromoCodeType.BALANCE_AND_DAYS.value


@pytest.mark.asyncio
async def test_promocode_prize_with_only_promo_group_gets_promo_group_type() -> None:
    """B2: чисто скидочный приз — код типа promo_group, а не пустой balance."""
    svc = FortuneWheelService()
    prize = _prize(promo_group_id=5)
    config = SimpleNamespace(promo_prefix='WHEEL', promo_validity_days=30)
    db = SimpleNamespace(add=lambda obj: None, flush=AsyncMock())

    created = await svc._generate_prize_promocode(db, SimpleNamespace(id=42), prize, config)

    assert created.type == PromoCodeType.PROMO_GROUP.value
    assert created.promo_group_id == 5


@pytest.mark.asyncio
async def test_traffic_prize_goes_through_traffic_purchase() -> None:
    """B3: сырой `traffic_limit_gb +=` стирается пересчётом лимита."""
    svc = FortuneWheelService()
    prize = _prize(prize_type=WheelPrizeType.TRAFFIC_GB.value, prize_value=25, prize_value_kopeks=2500)
    subscription = SimpleNamespace(id=7, traffic_limit_gb=500, updated_at=None)
    user = SimpleNamespace(id=42)
    add_traffic = AsyncMock()

    with (
        patch('app.database.crud.subscription.add_subscription_traffic', add_traffic),
        patch('app.services.wheel_service.SubscriptionService') as sub_service,
    ):
        sub_service.return_value.update_remnawave_user = AsyncMock()
        await svc._apply_prize(AsyncMock(), user, prize, SimpleNamespace(), subscription)

    add_traffic.assert_awaited_once()
    args, kwargs = add_traffic.await_args
    assert args[2] == 25
    # Спин держит user-lock и коммитит одним куском — вложенный commit его рвёт.
    assert kwargs['commit'] is False
    # Лимит руками больше не трогаем: его пересчитает add_subscription_traffic.
    assert subscription.traffic_limit_gb == 500


def test_promocode_message_states_contents_and_validity() -> None:
    """B4: голый код не говорит ни что внутри, ни что он сгорит."""
    svc = FortuneWheelService()
    prize = _prize(promo_subscription_days=30, promo_traffic_gb=100)

    message = svc._get_prize_message(prize, 'WHEELDEADBEEF', validity_days=30)

    assert 'WHEELDEADBEEF' in message
    assert '30 дней подписки' in message
    assert '100 ГБ' in message
    assert '30 дней' in message.split('\n')[-1]


def test_promocode_message_without_validity_stays_backward_compatible() -> None:
    svc = FortuneWheelService()
    prize = _prize(promo_balance_bonus_kopeks=10000)

    message = svc._get_prize_message(prize, 'WHEELCAFE')

    assert message.startswith('Поздравляем! Ваш промокод на 100₽ на баланс: WHEELCAFE')
    assert '\n' not in message
