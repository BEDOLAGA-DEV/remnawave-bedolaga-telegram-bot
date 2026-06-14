import pytest

from app.services.speedtest_settings_service import SpeedtestSettingsService as SSS


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(SSS, '_storage_path', tmp_path / 'speedtest_settings.json')
    monkeypatch.setattr(SSS, '_data', {})
    monkeypatch.setattr(SSS, '_loaded', False)
    yield


def test_defaults():
    assert SSS.is_enabled() is False
    assert SSS.get_host_mapping() == {}


def test_set_and_get_mapping():
    assert SSS.set_host_mapping({'uuid-1': 'node1.example.com'}) is True
    assert SSS.get_host_mapping() == {'uuid-1': 'node1.example.com'}


def test_mapping_rejects_non_dict():
    assert SSS.set_host_mapping('nope') is False


def test_resolve_host_strips_scheme_and_path():
    assert SSS.set_host_mapping({'u': 'https://node1.example.com/foo'}) is True
    assert SSS.get_host_mapping()['u'] == 'node1.example.com'


# --- name_mapping (custom display names) ---


def test_name_mapping_default():
    assert SSS.get_name_mapping() == {}


def test_set_and_get_name_mapping():
    assert SSS.set_name_mapping({'uuid-1': 'Москва-1'}) is True
    assert SSS.get_name_mapping() == {'uuid-1': 'Москва-1'}


def test_name_mapping_rejects_non_dict():
    assert SSS.set_name_mapping('nope') is False


def test_name_mapping_trims_and_drops_empty_or_non_str():
    assert SSS.set_name_mapping({'a': '  Берлин  ', 'b': '', 'c': '   ', 'd': 123}) is True
    # trimmed; empty / whitespace-only / non-string values dropped
    assert SSS.get_name_mapping() == {'a': 'Берлин'}


def test_name_mapping_caps_length():
    assert SSS.set_name_mapping({'a': 'X' * 200}) is True
    assert len(SSS.get_name_mapping()['a']) <= 64


def test_name_mapping_strips_control_chars():
    assert SSS.set_name_mapping({'a': 'NL\x00\x1f-1'}) is True
    assert SSS.get_name_mapping()['a'] == 'NL-1'
