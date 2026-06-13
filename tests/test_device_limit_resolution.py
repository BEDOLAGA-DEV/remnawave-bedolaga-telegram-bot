import pytest

from app.utils import subscription_utils
from app.utils.subscription_utils import (
    coerce_panel_device_limit,
    device_limit_needs_heal,
    resolve_hwid_device_limit,
    resolve_hwid_device_limit_for_payload,
    resolve_simple_subscription_device_limit,
)


class DummySubscription:
    def __init__(self, device_limit=None):
        self.device_limit = device_limit


class StubSettings:
    def __init__(
        self,
        enabled: bool,
        disabled_amount,
        *,
        simple_limit: int = 3,
        disabled_selection_amount=None,
    ):
        self._enabled = enabled
        self._disabled_amount = disabled_amount
        self._disabled_selection_amount = disabled_selection_amount
        self.SIMPLE_SUBSCRIPTION_DEVICE_LIMIT = simple_limit

    def is_devices_selection_enabled(self) -> bool:
        return self._enabled

    @staticmethod
    def _normalize_forced_amount(raw_value):
        # Mirror app.config.Settings.get_devices_selection_disabled_amount:
        # blank/None and any value <= 0 mean "no override" and collapse to None.
        if raw_value in (None, ''):
            return None
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        return value

    def get_disabled_mode_device_limit(self):
        return self._normalize_forced_amount(self._disabled_amount)

    def get_devices_selection_disabled_amount(self):
        return self._normalize_forced_amount(self._disabled_selection_amount)


@pytest.mark.parametrize(
    'forced_amount, expected',
    [
        # No usable override (None / non-positive collapse to "unset" in the real
        # config) -> fall through to the subscription's stored device_limit (42).
        (None, 42),
        (0, 42),
        # A positive override wins.
        (5, 5),
    ],
)
def test_resolve_hwid_device_limit_disabled_mode(monkeypatch, forced_amount, expected):
    subscription = DummySubscription(device_limit=42)

    monkeypatch.setattr(
        subscription_utils,
        'settings',
        StubSettings(
            enabled=False,
            disabled_amount=forced_amount,
            disabled_selection_amount=forced_amount,
        ),
    )

    assert resolve_hwid_device_limit(subscription) == expected


def test_resolve_hwid_device_limit_enabled_mode(monkeypatch):
    subscription = DummySubscription(device_limit=4)

    monkeypatch.setattr(
        subscription_utils,
        'settings',
        StubSettings(enabled=True, disabled_amount=None),
    )

    assert resolve_hwid_device_limit(subscription) == 4


def test_resolve_hwid_device_limit_enabled_ignores_non_positive(monkeypatch):
    subscription = DummySubscription(device_limit=0)

    monkeypatch.setattr(
        subscription_utils,
        'settings',
        StubSettings(enabled=True, disabled_amount=None),
    )

    assert resolve_hwid_device_limit(subscription) is None


def test_resolve_hwid_device_limit_for_payload_returns_subscription_limit(monkeypatch):
    subscription = DummySubscription(device_limit=42)

    monkeypatch.setattr(
        subscription_utils,
        'settings',
        StubSettings(enabled=False, disabled_amount=None, disabled_selection_amount=None),
    )

    # With selection disabled and no override, the subscription's stored limit now
    # flows straight through (no longer suppressed to None) so the panel stays aligned.
    assert resolve_hwid_device_limit(subscription) == 42
    assert resolve_hwid_device_limit_for_payload(subscription) == 42


def test_resolve_hwid_device_limit_for_payload_ignores_non_positive(monkeypatch):
    subscription = DummySubscription(device_limit=0)

    monkeypatch.setattr(
        subscription_utils,
        'settings',
        StubSettings(enabled=False, disabled_amount=None, disabled_selection_amount=None),
    )

    assert resolve_hwid_device_limit(subscription) is None
    assert resolve_hwid_device_limit_for_payload(subscription) is None


def test_resolve_hwid_device_limit_for_payload_prefers_forced_limit(monkeypatch):
    subscription = DummySubscription(device_limit=42)

    monkeypatch.setattr(
        subscription_utils,
        'settings',
        StubSettings(enabled=False, disabled_amount=7, disabled_selection_amount=7),
    )

    assert resolve_hwid_device_limit_for_payload(subscription) == 7


def test_resolve_hwid_device_limit_for_payload_handles_zero(monkeypatch):
    subscription = DummySubscription(device_limit=42)

    monkeypatch.setattr(
        subscription_utils,
        'settings',
        StubSettings(enabled=False, disabled_amount=0, disabled_selection_amount=0),
    )

    # A forced amount of 0 is normalized to "no override" by the real config, so the
    # subscription's stored limit (42) is used rather than being suppressed to 0.
    assert resolve_hwid_device_limit(subscription) == 42
    assert resolve_hwid_device_limit_for_payload(subscription) == 42


@pytest.mark.parametrize(
    'enabled, simple_limit, disabled_amount, disabled_selection_amount, expected',
    [
        (True, 4, None, None, 4),
        (False, 4, None, None, 4),
        # A forced amount of 0 normalizes to "no override" -> simple limit (4) is used.
        (False, 4, 0, 0, 4),
        (False, 4, 7, 7, 7),
    ],
)
def test_resolve_simple_subscription_device_limit(
    monkeypatch,
    enabled,
    simple_limit,
    disabled_amount,
    disabled_selection_amount,
    expected,
):
    monkeypatch.setattr(
        subscription_utils,
        'settings',
        StubSettings(
            enabled=enabled,
            disabled_amount=disabled_amount,
            simple_limit=simple_limit,
            disabled_selection_amount=disabled_selection_amount,
        ),
    )

    assert resolve_simple_subscription_device_limit() == expected


@pytest.mark.parametrize(
    'panel_value, expected',
    [
        (0, 0),
        (1, 1),
        (5, 5),
        (None, 1),
        ('', 1),
        ('5', 1),
        (True, 1),
        (False, 1),
        (-1, 1),
        (1.5, 1),
    ],
)
def test_coerce_panel_device_limit_preserves_zero_and_rejects_invalid(panel_value, expected):
    assert coerce_panel_device_limit(panel_value) == expected


@pytest.mark.parametrize(
    'panel_value, default, expected',
    [
        (None, 0, 0),
        (None, 7, 7),
        (0, 7, 0),
        ('bad', 9, 9),
    ],
)
def test_coerce_panel_device_limit_honors_default(panel_value, default, expected):
    assert coerce_panel_device_limit(panel_value, default=default) == expected


@pytest.mark.parametrize(
    'stored_value, needs_heal',
    [
        # Legitimate state — DO NOT heal. Without this guard, every sync pass
        # would revert unlimited-device subscriptions back to 1 device.
        (0, False),
        (1, False),
        (5, False),
        (100, False),
        # Structurally invalid — heal back to default.
        (None, True),
        (-1, True),
        (-100, True),
    ],
)
def test_device_limit_needs_heal_preserves_zero(stored_value, needs_heal):
    assert device_limit_needs_heal(stored_value) is needs_heal
