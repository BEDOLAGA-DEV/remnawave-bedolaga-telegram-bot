"""Older pricing entrypoints must retain the saved daily device billing policy."""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.subscription import tariff_purchase as telegram
from tests.services.test_tariff_purchase_quote import make_tariff


class PriceBoundary(BaseException):
    pass


@pytest.mark.parametrize('flow', ['renewal_preview', 'admin_purchase'])
async def test_existing_daily_price_uses_saved_policy(monkeypatch, flow):
    from app.handlers.admin import users as admin
    from app.services.pricing_engine import PricingEngine, pricing_engine

    tariff = make_tariff(is_daily=True, daily_price_kopeks=1000, device_price_kopeks=3000, is_active=True)
    subscription = SimpleNamespace(id=10, tariff_id=7, device_limit=3, daily_devices_billing_version=1)
    user = SimpleNamespace(id=1, telegram_id=101, language='ru', get_primary_promo_group=lambda: None)
    callback = SimpleNamespace(
        data='tariff_extend:10:7:1', answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock())
    )
    prices = []
    real_price = PricingEngine.calculate_tariff_purchase_price

    async def price(*args, **kwargs):
        result = await real_price(pricing_engine, *args, **kwargs)
        prices.append(result.final_total)
        raise PriceBoundary

    monkeypatch.setattr(pricing_engine, 'calculate_tariff_purchase_price', price)
    monkeypatch.setattr(telegram, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(telegram, 'get_subscription_by_id_for_user', AsyncMock(return_value=subscription))
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=user))
    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(admin.UserService, 'get_user_profile', AsyncMock(return_value={'user': user}))
    monkeypatch.setattr(admin, '_resolve_admin_subscription', AsyncMock(return_value=subscription))
    with pytest.raises(PriceBoundary):
        if flow == 'admin_purchase':
            callback.data = 'admin_buy_tariff_execute_1_7_1_1200'
            await inspect.unwrap(admin.admin_buy_tariff_execute)(callback, user, AsyncMock())
        else:
            await inspect.unwrap(telegram.select_tariff_extend_period)(callback, user, AsyncMock(), AsyncMock())
    assert prices == [1200]
