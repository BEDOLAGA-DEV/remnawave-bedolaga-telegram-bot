from app.handlers.admin.tariffs import _parse_device_price_tiers


def test_parse_basic():
    assert _parse_device_price_tiers('3:4000, 5:7000') == {'3': 4000, '5': 7000}


def test_parse_separators_and_base_excluded():
    # base count (1) ignored (must be >= 2); ';' and '=' accepted
    assert _parse_device_price_tiers('1:0; 3=4000') == {'3': 4000}


def test_parse_empty_and_garbage():
    assert _parse_device_price_tiers('') == {}
    assert _parse_device_price_tiers('abc, 3:x') == {}
