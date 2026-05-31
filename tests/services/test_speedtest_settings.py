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
