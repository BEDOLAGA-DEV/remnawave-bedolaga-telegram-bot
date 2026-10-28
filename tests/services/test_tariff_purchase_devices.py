from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.tariff_purchase_devices import DeviceSelectionError, resolve_purchase_device_selection


def tariff(**overrides):
    return SimpleNamespace(
        id=7, device_limit=1, device_price_kopeks=10000, max_device_limit=5, is_daily=False, **overrides
    )


def make_tariff(**overrides):
    data = vars(tariff()) | overrides
    return SimpleNamespace(**data)


@pytest.mark.parametrize('selected', [1, 3, 5])
def test_new_tariff_accepts_total_devices(selected):
    result = resolve_purchase_device_selection(tariff(), None, selected)
    assert result.effective_limit == selected
    assert result.included_limit == result.min_limit == 1
    assert result.max_limit == 5
    assert result.selectable


@pytest.mark.parametrize('selected', [0, -1, 6, True, 1.5, '3', 2147483648])
def test_invalid_selection_is_rejected(selected):
    with pytest.raises(DeviceSelectionError):
        resolve_purchase_device_selection(tariff(), None, selected)


@pytest.mark.parametrize('price', [None, 0, -1])
def test_tariff_without_extra_price_remains_fixed(price):
    fixed = make_tariff(device_price_kopeks=price)
    assert not resolve_purchase_device_selection(fixed, None, None).selectable
    with pytest.raises(DeviceSelectionError):
        resolve_purchase_device_selection(fixed, None, 2)


def test_global_classic_toggle_does_not_control_tariff_selection(monkeypatch):
    monkeypatch.setattr(settings, 'DEVICES_SELECTION_ENABLED', False)
    assert resolve_purchase_device_selection(tariff(), None, 3).effective_limit == 3


def test_unlimited_tariff_cannot_be_overridden():
    unlimited = make_tariff(device_limit=0)
    result = resolve_purchase_device_selection(unlimited, None, None)
    assert result.effective_limit == 0
    assert not result.selectable
    with pytest.raises(DeviceSelectionError):
        resolve_purchase_device_selection(unlimited, None, 3)


@pytest.mark.parametrize('status', ['active', 'expired', 'disabled', 'limited'])
def test_existing_paid_subscription_does_not_get_purchase_selector(status):
    subscription = SimpleNamespace(tariff_id=7, device_limit=4, is_trial=False, status=status)
    result = resolve_purchase_device_selection(tariff(), subscription, None)
    assert result.effective_limit == 4
    assert not result.selectable
    with pytest.raises(DeviceSelectionError):
        resolve_purchase_device_selection(tariff(), subscription, 5)


def test_trial_conversion_can_select_devices():
    subscription = SimpleNamespace(tariff_id=7, device_limit=1, is_trial=True)
    assert resolve_purchase_device_selection(tariff(), subscription, 3).effective_limit == 3


def test_classic_conversion_keeps_legacy_resolution():
    subscription = SimpleNamespace(tariff_id=None, device_limit=10, is_trial=False)
    result = resolve_purchase_device_selection(tariff(), subscription, None)
    assert result.effective_limit == 5
    assert not result.selectable
    with pytest.raises(DeviceSelectionError):
        resolve_purchase_device_selection(tariff(), subscription, 2)


def test_daily_selection_is_not_enabled_before_recurring_billing_support():
    daily = make_tariff(is_daily=True)
    assert not resolve_purchase_device_selection(daily, None, None).selectable
    with pytest.raises(DeviceSelectionError):
        resolve_purchase_device_selection(daily, None, 3)


def test_global_max_is_fallback_and_no_artificial_100_limit(monkeypatch):
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 150)
    item = make_tariff(max_device_limit=None)
    assert resolve_purchase_device_selection(item, None, 120).effective_limit == 120
    with pytest.raises(DeviceSelectionError):
        resolve_purchase_device_selection(item, None, 151)
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 0)
    result = resolve_purchase_device_selection(item, None, 200)
    assert result.effective_limit == 200
    assert result.max_limit == 2147483647


def test_bad_max_keeps_base_purchase_without_enabling_selector():
    item = make_tariff(device_limit=5, max_device_limit=2)
    result = resolve_purchase_device_selection(item, None, None)
    assert result.effective_limit == 5
    assert not result.selectable
    assert result.reason == 'invalid_device_configuration'


def test_missing_selection_preserves_default_purchase():
    assert resolve_purchase_device_selection(tariff(), None, None).effective_limit == 1
