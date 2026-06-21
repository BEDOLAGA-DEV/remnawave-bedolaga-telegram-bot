from app.cabinet.schemas.tariffs import TariffCreateRequest, TariffUpdateRequest


def test_create_request_accepts_tiers():
    req = TariffCreateRequest(name='t', device_price_tiers={'3': 4000, '5': 7000})
    assert req.device_price_tiers == {'3': 4000, '5': 7000}


def test_create_request_defaults_empty():
    req = TariffCreateRequest(name='t')
    assert req.device_price_tiers == {}


def test_update_request_optional():
    req = TariffUpdateRequest()
    assert req.device_price_tiers is None
    req2 = TariffUpdateRequest(device_price_tiers={'2': 2000})
    assert req2.device_price_tiers == {'2': 2000}
