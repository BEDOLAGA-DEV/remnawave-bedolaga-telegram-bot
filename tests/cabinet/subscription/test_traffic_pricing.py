"""Unit tests for traffic_pricing.calculate_traffic_reset_price."""

from unittest.mock import MagicMock, patch

import pytest


def test_reset_price_period_mode(make_subscription):
    from app.utils import traffic_pricing as tp

    sub = make_subscription()
    fake_settings = MagicMock()
    fake_settings.get_traffic_reset_price_mode.return_value = 'period'
    fake_settings.get_traffic_reset_base_price.return_value = 9000
    fake_settings.get_wl_traffic_price.return_value = 0

    with patch.object(tp, 'settings', fake_settings):
        price = tp.calculate_traffic_reset_price(sub, kind='wl')
        assert price == 9000


def test_reset_price_traffic_mode(make_subscription):
    from app.utils import traffic_pricing as tp

    sub = make_subscription(wl_traffic_limit_gb=50)
    fake_settings = MagicMock()
    fake_settings.get_traffic_reset_price_mode.return_value = 'traffic'
    fake_settings.get_traffic_reset_base_price.return_value = 1000
    fake_settings.get_wl_traffic_price.return_value = 5000

    with patch.object(tp, 'settings', fake_settings):
        price = tp.calculate_traffic_reset_price(sub, kind='wl')
        assert price == 5000  # max(5000, 1000)


def test_reset_price_traffic_with_purchased_mode(make_subscription):
    from app.utils import traffic_pricing as tp

    sub = make_subscription(wl_traffic_limit_gb=70, wl_purchased_traffic_gb=20)

    fake_settings = MagicMock()
    fake_settings.get_traffic_reset_price_mode.return_value = 'traffic_with_purchased'
    fake_settings.get_traffic_reset_base_price.return_value = 0

    def _wl_price(gb):
        return {50: 4000, 20: 2000}.get(gb, 0)

    fake_settings.get_wl_traffic_price.side_effect = _wl_price

    with patch.object(tp, 'settings', fake_settings), patch.object(tp, 'PERIOD_PRICES', {}):
        price = tp.calculate_traffic_reset_price(sub, kind='wl')
        assert price == 6000  # 4000 + 2000


def test_reset_price_unknown_mode_falls_back_to_base(make_subscription):
    from app.utils import traffic_pricing as tp

    sub = make_subscription()
    fake_settings = MagicMock()
    fake_settings.get_traffic_reset_price_mode.return_value = 'something_else'
    fake_settings.get_traffic_reset_base_price.return_value = 12345
    fake_settings.get_wl_traffic_price.return_value = 0

    with patch.object(tp, 'settings', fake_settings):
        price = tp.calculate_traffic_reset_price(sub, kind='wl')
        assert price == 12345
