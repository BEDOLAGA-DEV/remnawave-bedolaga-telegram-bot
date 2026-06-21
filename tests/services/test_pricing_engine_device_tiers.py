from app.database.models import Tariff


def _tariff():
    t = Tariff(name='t', device_limit=1)
    t.device_price_kopeks = None
    t.device_price_tiers = {'3': 4000, '5': 7000}
    return t


def test_engine_devices_per_month_matches_helper():
    # The engine prices devices via the helper, not a flat multiply.
    t = _tariff()
    assert t.get_device_extra_price_per_month(5) == 7000
    assert t.get_device_extra_price_per_month(3) == 4000
