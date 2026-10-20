"""Закрытый тариф нельзя оплатить без подходящей группы, даже старой кнопкой."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.database.models import PromoGroup, Tariff


def _tariff(*groups: int) -> Tariff:
    tariff = Tariff(
        id=9,
        name='Restricted',
        is_active=True,
        period_prices={'30': 10000},
        is_daily=True,
        daily_price_kopeks=100,
        device_limit=1,
        traffic_limit_gb=100,
    )
    tariff.allowed_promo_groups = [PromoGroup(id=i, name=f'Group {i}') for i in groups]
    return tariff


@pytest.mark.parametrize(
    ('groups', 'user_group', 'expected'),
    [
        ((), None, True),
        ((), 10, True),
        ((10,), None, False),
        ((10,), 11, False),
        ((10,), 10, True),
        ((10, 12), 12, True),
    ],
)
def test_tariff_promo_group_authorization(groups, user_group, expected):
    assert _tariff(*groups).is_available_for_promo_group(user_group) is expected


@pytest.mark.parametrize(
    'handler_name',
    [
        'confirm_tariff_purchase',
        'handle_custom_confirm',
        'confirm_daily_tariff_purchase',
        'confirm_tariff_switch',
        'confirm_daily_tariff_switch',
        'confirm_instant_switch',
    ],
)
async def test_stale_telegram_button_denied_before_checkout(monkeypatch, handler_name):
    from app.database.crud import user as users
    from app.handlers.subscription import tariff_purchase as m

    # The incoming object can contain an old group; the locked account is authoritative.
    incoming = SimpleNamespace(id=42, language='ru', promo_group=PromoGroup(id=10, name='Old'))
    locked = SimpleNamespace(id=42, language='ru', promo_group=None, balance_kopeks=10000)
    callback = SimpleNamespace(data='confirm:9:30', answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
    db = AsyncMock()
    db.execute.side_effect = AssertionError('must reject before database checkout')
    state = SimpleNamespace(get_data=AsyncMock(side_effect=AssertionError('must reject before checkout')))
    monkeypatch.setattr(m, 'get_tariff_by_id', AsyncMock(return_value=_tariff(10)))
    monkeypatch.setattr(users, 'lock_user_for_pricing', AsyncMock(return_value=locked))
    monkeypatch.setattr(m, '_resolve_switch_subscription', AsyncMock(return_value=(SimpleNamespace(tariff_id=9), 9)))
    handler = getattr(m, handler_name)
    while hasattr(handler, '__wrapped__'):
        handler = handler.__wrapped__

    await handler(callback, incoming, db, state)

    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs['show_alert'] is True
    assert 'недоступен' in callback.answer.await_args.args[0].lower()
    assert locked.balance_kopeks == 10000
    db.commit.assert_not_awaited()
    state.get_data.assert_not_awaited()


@pytest.mark.parametrize('platform', ['cabinet', 'miniapp'])
async def test_api_purchase_denied_without_group(monkeypatch, platform):
    from app.config import settings
    from app.database.crud import user as users

    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    user = SimpleNamespace(id=42, language='ru', balance_kopeks=10000, get_primary_promo_group=lambda: None)
    monkeypatch.setattr(users, 'lock_user_for_pricing', AsyncMock(return_value=user))
    db = AsyncMock()
    if platform == 'cabinet':
        from app.cabinet.routes.subscription_modules import purchase as m
        from app.cabinet.schemas.subscription import TariffPurchaseRequest

        monkeypatch.setattr(m, 'get_tariff_by_id', AsyncMock(return_value=_tariff(10)))
        call = m.purchase_tariff(TariffPurchaseRequest(tariff_id=9, period_days=30), user, db)
    else:
        from app.webapi.routes import miniapp as m
        from app.webapi.schemas.miniapp import MiniAppTariffPurchaseRequest

        monkeypatch.setattr(m, 'get_tariff_by_id', AsyncMock(return_value=_tariff(10)))
        monkeypatch.setattr(m, '_authorize_miniapp_user', AsyncMock(return_value=user))
        call = m.purchase_tariff_endpoint(
            MiniAppTariffPurchaseRequest(initData='test', tariffId=9, periodDays=30), db
        )
    with pytest.raises(HTTPException) as exc:
        await call
    assert exc.value.status_code == 403
    assert user.balance_kopeks == 10000
    db.commit.assert_not_awaited()
