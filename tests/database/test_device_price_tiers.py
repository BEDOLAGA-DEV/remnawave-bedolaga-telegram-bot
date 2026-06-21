from app.database.models import Tariff


def _tariff(device_limit=1, device_price_kopeks=None, tiers=None):
    t = Tariff(name='t', device_limit=device_limit)
    t.device_price_kopeks = device_price_kopeks
    t.device_price_tiers = tiers if tiers is not None else {}
    return t


def test_tiers_target_grid():
    t = _tariff(tiers={'3': 4000, '5': 7000})
    assert t.get_device_extra_price_per_month(1) == 0
    assert t.get_device_extra_price_per_month(2) == 2000
    assert t.get_device_extra_price_per_month(3) == 4000
    assert t.get_device_extra_price_per_month(4) == 5500
    assert t.get_device_extra_price_per_month(5) == 7000


def test_tiers_extrapolate_above_top_anchor():
    t = _tariff(tiers={'3': 4000, '5': 7000})
    assert t.get_device_extra_price_per_month(7) == 10000


def test_base_devices_free():
    t = _tariff(device_limit=2, tiers={'5': 7000})
    assert t.get_device_extra_price_per_month(1) == 0
    assert t.get_device_extra_price_per_month(2) == 0


def test_single_anchor_interpolates_from_base():
    t = _tariff(device_limit=1, tiers={'5': 8000})
    assert t.get_device_extra_price_per_month(3) == 4000


def test_linear_fallback_when_no_tiers():
    t = _tariff(device_limit=1, device_price_kopeks=5000, tiers={})
    assert t.get_device_extra_price_per_month(3) == 10000


def test_empty_everything_is_free():
    t = _tariff(device_limit=1, device_price_kopeks=None, tiers={})
    assert t.get_device_extra_price_per_month(3) == 0
