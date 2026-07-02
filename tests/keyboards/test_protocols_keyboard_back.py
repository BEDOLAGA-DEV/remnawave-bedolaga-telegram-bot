from app.config import settings
from app.keyboards.inline import get_manage_protocols_keyboard


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_back_goes_to_subscription_detail_in_multitariff(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True, raising=False)
    kb = get_manage_protocols_keyboard([{'uuid': 'a', 'name': 'A'}], ['a'], 'ru', sub_id=7)
    cbs = _cbs(kb)
    assert 'nz!_sm:7' in cbs
    assert 'nz!_subscription_settings' not in cbs


def test_back_goes_to_settings_in_classic_mode(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False, raising=False)
    kb = get_manage_protocols_keyboard([{'uuid': 'a', 'name': 'A'}], ['a'], 'ru', sub_id=7)
    cbs = _cbs(kb)
    assert 'nz!_subscription_settings' in cbs
    assert 'nz!_sm:7' not in cbs
