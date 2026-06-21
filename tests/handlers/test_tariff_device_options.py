from app.database.models import Tariff
from app.handlers.subscription.tariff_purchase import _tariff_device_purchase_options


def _t(device_limit=1, max_device_limit=None, tiers=None, price=None):
    t = Tariff(name='t', device_limit=device_limit)
    t.max_device_limit = max_device_limit
    t.device_price_tiers = tiers if tiers is not None else {}
    t.device_price_kopeks = price
    return t


def test_selectable_with_tiers_and_max():
    t = _t(1, 5, tiers={'3': 4000, '5': 7000})
    assert _tariff_device_purchase_options(t) == (True, 1, 5)


def test_not_selectable_without_price():
    t = _t(1, 5)  # no tiers, no price
    assert _tariff_device_purchase_options(t) == (False, 1, 5)


def test_not_selectable_when_max_not_above_base():
    t = _t(1, 1, tiers={'3': 4000})
    assert _tariff_device_purchase_options(t) == (False, 1, 1)


def test_linear_price_makes_selectable():
    t = _t(1, 3, price=500)
    assert _tariff_device_purchase_options(t) == (True, 1, 3)
