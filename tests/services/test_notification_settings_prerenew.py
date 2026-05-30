import pytest

from app.services.notification_settings_service import NotificationSettingsService as NSS


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    # Point the on-disk store at a temp file and reset the class cache.
    monkeypatch.setattr(NSS, '_storage_path', tmp_path / 'notification_settings.json')
    monkeypatch.setattr(NSS, '_data', {})
    monkeypatch.setattr(NSS, '_loaded', False)
    yield


def test_prerenew_save_defaults_are_off_with_expected_numbers():
    assert NSS.is_prerenew_save_enabled() is False
    assert NSS.get_prerenew_save_discount_percent() == 15
    assert NSS.get_prerenew_save_valid_hours() == 24
    assert NSS.get_prerenew_save_trigger_hours() == 36


def test_prerenew_save_setters_roundtrip():
    assert NSS.set_prerenew_save_enabled(True) is True
    assert NSS.is_prerenew_save_enabled() is True

    assert NSS.set_prerenew_save_discount_percent(25) is True
    assert NSS.get_prerenew_save_discount_percent() == 25

    assert NSS.set_prerenew_save_valid_hours(48) is True
    assert NSS.get_prerenew_save_valid_hours() == 48

    assert NSS.set_prerenew_save_trigger_hours(12) is True
    assert NSS.get_prerenew_save_trigger_hours() == 12


def test_prerenew_save_values_are_clamped_and_validated():
    NSS.set_prerenew_save_discount_percent(999)
    assert NSS.get_prerenew_save_discount_percent() == 100

    assert NSS.set_prerenew_save_discount_percent('abc') is False

    NSS.set_prerenew_save_valid_hours(0)
    assert NSS.get_prerenew_save_valid_hours() == 1

    NSS.set_prerenew_save_trigger_hours(99999)
    assert NSS.get_prerenew_save_trigger_hours() == 168
