import pytest

from app.services.freeze_settings_service import FreezeSettingsService as FSS


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(FSS, '_storage_path', tmp_path / 'freeze_settings.json')
    monkeypatch.setattr(FSS, '_data', {})
    monkeypatch.setattr(FSS, '_loaded', False)
    yield


def test_defaults():
    assert FSS.is_enabled() is False
    assert FSS.get_max_days_per_year() == 30
    assert FSS.get_min_subscription_age_days() == 7
    assert FSS.get_cooldown_days() == 7
    assert FSS.get_min_freeze_days() == 3
    assert FSS.get_max_single_freeze_days() == 30


def test_setters_roundtrip():
    assert FSS.set_enabled(True) is True
    assert FSS.is_enabled() is True
    assert FSS.set_max_days_per_year(60) is True
    assert FSS.get_max_days_per_year() == 60
    assert FSS.set_cooldown_days(0) is True
    assert FSS.get_cooldown_days() == 0


def test_validation_and_clamp():
    assert FSS.set_max_days_per_year('x') is False
    FSS.set_max_days_per_year(99999)
    assert FSS.get_max_days_per_year() == 365
    FSS.set_min_freeze_days(0)
    assert FSS.get_min_freeze_days() == 1
