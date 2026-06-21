from app.database.models import Tariff


def _t(periods):
    t = Tariff(name='t', device_limit=1)
    t.period_prices = periods
    return t


GRID = {'30': 3000, '90': 7000, '180': 10000}  # kopeks; 30/70/100 ₽


def test_exact_anchors():
    t = _t(GRID)
    assert t.get_price_for_days_anchored(30) == 3000
    assert t.get_price_for_days_anchored(90) == 7000
    assert t.get_price_for_days_anchored(180) == 10000


def test_between_anchors_floor_rate():
    t = _t(GRID)
    assert t.get_price_for_days_anchored(50) == 5000   # 50 * (3000/30) = 5000
    assert t.get_price_for_days_anchored(120) == 9300  # 120 * (7000/90) = 9333 -> round ruble 9300


def test_cap_at_next_anchor():
    t = _t(GRID)
    assert t.get_price_for_days_anchored(80) == 7000   # 80*100=8000 capped at ceil(90)=7000
    assert t.get_price_for_days_anchored(179) == 10000  # 179*77.7=13922 capped at ceil(180)=10000


def test_clamp_out_of_range():
    t = _t(GRID)
    assert t.get_price_for_days_anchored(10) == 3000   # clamp up to 30
    assert t.get_price_for_days_anchored(999) == 10000  # clamp down to 180


def test_single_anchor():
    t = _t({'30': 3000})
    assert t.get_price_for_days_anchored(45) == 3000   # clamped to the only anchor
    assert t.get_price_for_days_anchored(20) == 3000


def test_empty_prices():
    t = _t({})
    assert t.get_price_for_days_anchored(50) == 0
