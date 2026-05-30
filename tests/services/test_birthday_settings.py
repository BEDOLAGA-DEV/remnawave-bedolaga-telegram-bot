import pytest

from app.services.birthday_settings_service import BirthdaySettingsService as BSS


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(BSS, '_storage_path', tmp_path / 'birthday_settings.json')
    monkeypatch.setattr(BSS, '_data', {})
    monkeypatch.setattr(BSS, '_loaded', False)
    yield


def test_defaults():
    assert BSS.is_enabled() is False
    assert BSS.get_reward_type() == 'balance'
    assert BSS.get_reward_amount() == 10000
    assert BSS.get_min_account_age_days() == 7
    assert BSS.get_dob_stable_days() == 7
    assert BSS.get_promocode_valid_days() == 7
    assert BSS.get_subscription_days_fallback() == 'balance'


def test_setters_roundtrip():
    assert BSS.set_enabled(True) is True
    assert BSS.is_enabled() is True
    assert BSS.set_reward_type('promocode') is True
    assert BSS.get_reward_type() == 'promocode'
    assert BSS.set_reward_amount(500) is True
    assert BSS.get_reward_amount() == 500
    assert BSS.set_min_account_age_days(14) is True
    assert BSS.get_min_account_age_days() == 14
    assert BSS.set_subscription_days_fallback('skip') is True
    assert BSS.get_subscription_days_fallback() == 'skip'


def test_validation_rejects_bad_values():
    assert BSS.set_reward_type('bogus') is False
    assert BSS.get_reward_type() == 'balance'  # unchanged
    assert BSS.set_reward_amount(-5) is False
    assert BSS.set_subscription_days_fallback('nonsense') is False
    BSS.set_min_account_age_days(99999)
    assert BSS.get_min_account_age_days() == 365  # clamped
