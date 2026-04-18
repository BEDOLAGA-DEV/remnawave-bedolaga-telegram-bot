"""User notification preferences helper.

Reads notification_settings JSON from User model and provides
typed access to individual preferences with sensible defaults.
"""

from __future__ import annotations

from typing import Any

from app.database.models import User


# Defaults match the frontend and cabinet/routes/notifications.py
_DEFAULTS: dict[str, Any] = {
    'subscription_expiry_enabled': True,
    'subscription_expiry_days': 3,
    'traffic_warning_enabled': True,
    'traffic_warning_percent': 80,
    'balance_low_enabled': False,
    'balance_low_threshold': 100,  # kopeks
    'news_enabled': True,
    'promo_offers_enabled': True,
}


def get_user_notification_pref(user: User, key: str) -> Any:
    """Get a single notification preference for user.

    Falls back to default if not set.
    """
    settings_data = getattr(user, 'notification_settings', None) or {}
    return settings_data.get(key, _DEFAULTS.get(key))


def is_subscription_expiry_enabled(user: User) -> bool:
    """Check if subscription expiry notifications are enabled for user."""
    return bool(get_user_notification_pref(user, 'subscription_expiry_enabled'))


def get_subscription_expiry_days(user: User) -> int:
    """Get the number of days before expiry to notify."""
    value = get_user_notification_pref(user, 'subscription_expiry_days')
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 3


def is_traffic_warning_enabled(user: User) -> bool:
    """Check if traffic warning notifications are enabled for user."""
    return bool(get_user_notification_pref(user, 'traffic_warning_enabled'))


def get_traffic_warning_percent(user: User) -> int:
    """Get the traffic usage percentage threshold for warning."""
    value = get_user_notification_pref(user, 'traffic_warning_percent')
    try:
        return max(50, min(99, int(value)))
    except (TypeError, ValueError):
        return 80


def is_balance_low_enabled(user: User) -> bool:
    """Check if low balance notifications are enabled for user."""
    return bool(get_user_notification_pref(user, 'balance_low_enabled'))


def get_balance_low_threshold(user: User) -> int:
    """Get the low balance threshold in kopeks."""
    value = get_user_notification_pref(user, 'balance_low_threshold')
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 100


def is_news_enabled(user: User) -> bool:
    """Check if news notifications are enabled for user."""
    return bool(get_user_notification_pref(user, 'news_enabled'))


def is_promo_offers_enabled(user: User) -> bool:
    """Check if promo offer notifications are enabled for user."""
    return bool(get_user_notification_pref(user, 'promo_offers_enabled'))
