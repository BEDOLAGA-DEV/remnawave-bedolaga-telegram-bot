from datetime import UTC, datetime
from types import SimpleNamespace

from app.handlers.start import _calculate_subscription_flags


def _sub(actual_status, is_active, frozen_at=None):
    return SimpleNamespace(actual_status=actual_status, is_active=is_active, frozen_at=frozen_at)


def test_flags_none_subscription():
    assert _calculate_subscription_flags(None) == (False, False)


def test_flags_active_subscription():
    assert _calculate_subscription_flags(_sub('active', True)) == (True, True)


def test_flags_limited_is_active_for_menu():
    # traffic exhausted but not expired — still active for UI
    assert _calculate_subscription_flags(_sub('limited', False)) == (True, True)


def test_flags_expired_is_inactive():
    assert _calculate_subscription_flags(_sub('expired', False)) == (False, False)


def test_flags_frozen_disabled_still_active_for_menu():
    # A frozen subscription whose panel echo desynced status to DISABLED must
    # still count as active-for-menu, otherwise the main-menu "Подписка" button
    # disappears and the user has no way to reach the subscription to unfreeze.
    sub = _sub('disabled', False, frozen_at=datetime.now(UTC))
    assert _calculate_subscription_flags(sub) == (True, True)
