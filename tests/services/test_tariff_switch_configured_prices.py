from types import SimpleNamespace

import pytest

from app.services.pricing_engine import TariffSwitchPricingUnavailableError, pricing_engine


def tariff(prices, daily=False):
    return SimpleNamespace(period_prices=prices, is_daily=daily, daily_price_kopeks=100)


@pytest.mark.parametrize('prices', [{}, {'30': None}, {'0': 100}, {'bad': 100}, {'30': -1}])
@pytest.mark.parametrize('daily_source', [False, True])
def test_unconfigured_target_is_not_free(prices, daily_source):
    with pytest.raises(TariffSwitchPricingUnavailableError):
        pricing_engine.calculate_tariff_switch_cost(tariff({'30': 1000}, daily_source), tariff(prices), 10)


@pytest.mark.parametrize('daily_target', [False, True])
@pytest.mark.parametrize('remaining', [0, 10])
def test_unconfigured_source_rejected_before_shortcuts(daily_target, remaining):
    with pytest.raises(TariffSwitchPricingUnavailableError):
        pricing_engine.calculate_tariff_switch_cost(tariff({}), tariff({'30': 1000}, daily_target), remaining)


@pytest.mark.parametrize('source_daily', [False, True])
def test_explicit_free_period_stays_valid(source_daily):
    result = pricing_engine.calculate_tariff_switch_cost(tariff({'30': 1000}, source_daily), tariff({'30': 0}), 10)
    assert result.upgrade_cost == 0
    if source_daily:
        assert result.new_period_days == 30


def test_null_shorter_period_does_not_override_configured_price():
    target = tariff({'14': None, '30': 3000})
    assert pricing_engine.get_tariff_daily_rate_fraction(target) == (3000, 30)
    result = pricing_engine.calculate_tariff_switch_cost(tariff({}, True), target, 10)
    assert result.upgrade_cost == 3000
    assert result.new_period_days == 30


def test_miniapp_wrapper_returns_client_error():
    from fastapi import HTTPException

    from app.webapi.routes.miniapp import _calculate_tariff_switch

    with pytest.raises(HTTPException) as raised:
        _calculate_tariff_switch(tariff({'30': 1000}), tariff({}), 10)
    assert raised.value.status_code == 400


def test_telegram_list_omits_unconfigured_tariff(monkeypatch):
    from app.config import settings
    from app.database.models import Tariff
    from app.handlers.subscription.tariff_purchase import (
        _filter_tariffs_by_switch_direction,
        format_instant_switch_list_text,
        get_instant_switch_keyboard,
    )

    monkeypatch.setattr(settings, 'TARIFF_SWITCH_UPGRADE_ENABLED', True)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_DOWNGRADE_ENABLED', True)
    source = Tariff(id=1, name='Current', is_daily=False, period_prices={'30': 1000})
    target = Tariff(id=2, name='Unavailable', is_daily=False, period_prices={}, traffic_limit_gb=1)
    assert _filter_tariffs_by_switch_direction([target], source, 10) == []
    assert 'Unavailable' not in format_instant_switch_list_text([target], source, 10)
    keyboard = get_instant_switch_keyboard([target], source, 10, 'ru')
    assert all(button.callback_data != 'instant_sw_preview:2' for row in keyboard.inline_keyboard for button in row)


def test_legacy_none_current_with_promo_group_uses_thirty_day_discount():
    from unittest.mock import Mock

    discount = Mock(return_value=10)
    group = SimpleNamespace(id=1, get_discount_percent=discount)
    user = SimpleNamespace(
        promo_group=group,
        get_primary_promo_group=lambda: group,
        promo_offer_discount_percent=0,
        promo_offer_discount_expires_at=None,
    )
    result = pricing_engine.calculate_tariff_switch_cost(None, tariff({'30': 3000}), 10, user=user)
    assert result.raw_cost == 1000
    assert result.upgrade_cost == 900
    discount.assert_called_once_with('period', 30)
