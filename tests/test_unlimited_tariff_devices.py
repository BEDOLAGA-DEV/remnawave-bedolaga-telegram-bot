"""Ноль устройств у тарифа означает безлимит, а не отсутствие настройки."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import Tariff
from app.services.pricing_engine import PricingEngine
from app.utils import subscription_utils


@pytest.mark.parametrize(
    ('base', 'current', 'expected'),
    [
        (0, 1, 0),
        (0, 5, 0),
        (0, 0, 0),
        (0, None, 0),
        (2, 5, 5),
        (2, 1, 2),
        (2, 0, 2),
        (None, 5, 5),
        (None, None, 0),
    ],
)
def test_tariff_renewal_device_limit(base, current, expected):
    assert subscription_utils.resolve_tariff_renewal_device_limit(base, current) == expected


@pytest.mark.parametrize('current', [1, 5])
@pytest.mark.parametrize(('days', 'daily', 'base_price'), [(1, True, 100), (30, False, 10000), (60, False, 18000)])
async def test_unlimited_tariff_does_not_charge_device_addons(current, days, daily, base_price):
    tariff = Tariff(
        id=1,
        name='Unlimited',
        device_limit=0,
        device_price_kopeks=5000,
        period_prices={'30': 10000, '60': 18000},
        is_daily=daily,
        daily_price_kopeks=100,
    )
    result = await PricingEngine().calculate_tariff_purchase_price(tariff, days, device_limit=current)
    assert result.devices_price == 0
    assert result.final_total == base_price


@pytest.mark.parametrize(('new_base', 'cap', 'expected'), [(0, 2, 0), (2, 5, 5)])
async def test_admin_tariff_change_preserves_unlimited(monkeypatch, new_base, cap, expected):
    from app.database.crud import transaction
    from app.handlers.admin import users as m

    old = SimpleNamespace(id=1, device_limit=1)
    tariff = SimpleNamespace(
        id=2, name='Target', device_limit=new_base, max_device_limit=cap, traffic_limit_gb=100, allowed_squads=[]
    )
    subscription = SimpleNamespace(id=7, tariff_id=1, device_limit=5)
    user = SimpleNamespace(id=42)
    monkeypatch.setattr(m, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(m, 'get_tariff_by_id', AsyncMock(side_effect=[tariff, old]))
    monkeypatch.setattr(m, '_resolve_admin_subscription', AsyncMock(return_value=subscription))
    monkeypatch.setattr(transaction, 'create_transaction', AsyncMock())
    sync = AsyncMock(return_value=True)
    monkeypatch.setattr(m.SubscriptionService, 'update_remnawave_user', sync)
    callback = SimpleNamespace(
        data='admin_sub_tariff_confirm_2_42', answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock())
    )
    handler = m.confirm_admin_tariff_change
    while hasattr(handler, '__wrapped__'):
        handler = handler.__wrapped__
    db = AsyncMock()
    await handler(callback, SimpleNamespace(id=1), db)
    assert subscription.device_limit == expected
    sync.assert_awaited_once()
    assert sync.await_args.args[1].device_limit == expected
    db.rollback.assert_not_awaited()
