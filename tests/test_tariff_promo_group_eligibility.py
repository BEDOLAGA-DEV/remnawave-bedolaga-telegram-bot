"""Промогруппы тарифа — правило авторизации покупки, а не только фильтр каталога."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import PromoGroup, Tariff
from app.handlers.subscription import tariff_purchase


def _tariff(*group_ids: int) -> Tariff:
    tariff = Tariff(
        id=9,
        name='Ограниченный',
        is_active=True,
        traffic_limit_gb=100,
        period_prices={'30': 10000},
        is_daily=False,
    )
    tariff.allowed_promo_groups = [
        PromoGroup(id=group_id, name=f'Группа {group_id}') for group_id in group_ids
    ]
    return tariff


@pytest.mark.parametrize(
    ('allowed', 'user_group_id', 'expected'),
    [
        ((), None, True),   # Публичный тариф доступен без промогруппы.
        ((), 10, True),     # Публичный тариф доступен любой промогруппе.
        ((10,), None, False),  # Закрытый тариф нельзя купить без группы.
        ((10,), 11, False),    # Чужая группа не даёт доступ.
        ((10,), 10, True),     # Своя группа даёт доступ.
        ((10, 12), 12, True),
    ],
)
def test_tariff_promo_group_authorization(allowed, user_group_id, expected):
    tariff = _tariff(*allowed)
    assert tariff.is_available_for_promo_group(user_group_id) is expected


@pytest.mark.asyncio
async def test_telegram_guard_denies_restricted_tariff_without_group():
    callback = SimpleNamespace(answer=AsyncMock())
    texts = SimpleNamespace(t=lambda _key, default: default)
    user = SimpleNamespace(promo_group=None)

    blocked = await tariff_purchase._reject_unavailable_tariff(
        callback, _tariff(10), user, texts
    )

    assert blocked is True
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs['show_alert'] is True
    assert callback.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_telegram_guard_allows_matching_group():
    callback = SimpleNamespace(answer=AsyncMock())
    texts = SimpleNamespace(t=lambda _key, default: default)
    group = PromoGroup(id=10, name='Группа 10')
    user = SimpleNamespace(promo_group=group)

    blocked = await tariff_purchase._reject_unavailable_tariff(
        callback, _tariff(10), user, texts
    )

    assert blocked is False
    callback.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_purchase_rejects_stale_button_before_pricing(monkeypatch):
    """Скрытый тариф нельзя оплатить через callback из старого сообщения."""
    from app.database.crud import user as crud_user
    from app.services.pricing_engine import pricing_engine

    user = SimpleNamespace(id=42, language='ru', promo_group=None)
    callback = SimpleNamespace(data='tariff_confirm:9:30', answer=AsyncMock())
    get_tariff = AsyncMock(return_value=_tariff(10))
    lock_user = AsyncMock(return_value=user)
    pricing = AsyncMock(side_effect=AssertionError('pricing must not be called'))

    monkeypatch.setattr(tariff_purchase, 'get_tariff_by_id', get_tariff)
    monkeypatch.setattr(crud_user, 'lock_user_for_pricing', lock_user)
    monkeypatch.setattr(pricing_engine, 'calculate_tariff_purchase_price', pricing)

    await tariff_purchase.confirm_tariff_purchase(callback, user, object(), None)

    pricing.assert_not_awaited()
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs['show_alert'] is True
    assert callback.answer.await_args.args[0]
