from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.handlers.start import _RETROACTIVE_REFERRAL_WINDOW, _within_retroactive_referral_window


def _user(created_ago: timedelta | None) -> SimpleNamespace:
    created_at = None if created_ago is None else datetime.now(UTC) - created_ago
    return SimpleNamespace(created_at=created_at)


def test_user_created_by_miniapp_seconds_before_start_gets_referrer():
    assert _within_retroactive_referral_window(_user(timedelta(seconds=30)))


def test_user_inside_window_gets_referrer():
    assert _within_retroactive_referral_window(_user(_RETROACTIVE_REFERRAL_WINDOW - timedelta(seconds=5)))


def test_long_registered_user_does_not_become_referral_by_clicking_link():
    assert not _within_retroactive_referral_window(_user(timedelta(days=200)))


def test_user_just_outside_window_does_not_become_referral():
    assert not _within_retroactive_referral_window(_user(_RETROACTIVE_REFERRAL_WINDOW + timedelta(seconds=5)))


def test_user_without_created_at_does_not_become_referral():
    assert not _within_retroactive_referral_window(_user(None))
