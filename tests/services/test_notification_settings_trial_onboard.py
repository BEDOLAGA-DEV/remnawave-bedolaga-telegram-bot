import pytest

from app.services.notification_settings_service import NotificationSettingsService as NSS


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(NSS, '_storage_path', tmp_path / 'notification_settings.json')
    monkeypatch.setattr(NSS, '_data', {})
    monkeypatch.setattr(NSS, '_loaded', False)
    yield


def test_trial_onboard_defaults_off_with_expected_hours():
    assert NSS.is_trial_onboard_enabled() is False
    assert NSS.get_trial_onboard_first_hours() == 3
    assert NSS.get_trial_onboard_second_hours() == 12


def test_trial_onboard_setters_roundtrip():
    assert NSS.set_trial_onboard_enabled(True) is True
    assert NSS.is_trial_onboard_enabled() is True

    assert NSS.set_trial_onboard_first_hours(6) is True
    assert NSS.get_trial_onboard_first_hours() == 6

    assert NSS.set_trial_onboard_second_hours(24) is True
    assert NSS.get_trial_onboard_second_hours() == 24


def test_trial_onboard_hours_clamped_and_validated():
    NSS.set_trial_onboard_first_hours(0)
    assert NSS.get_trial_onboard_first_hours() == 1

    NSS.set_trial_onboard_second_hours(99999)
    assert NSS.get_trial_onboard_second_hours() == 168

    assert NSS.set_trial_onboard_first_hours('abc') is False
