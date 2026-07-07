from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from app.handlers.menu import _get_subscription_status


class DummyTexts:
    def t(self, key: str, default: str):  # pragma: no cover - simple stub
        return default


def _build_user_with_subscription(
    actual_status: str,
    is_trial: bool,
    days_left: int,
    *,
    is_bio: bool = False,
    hours: int = 1,
):
    subscription = MagicMock()
    subscription.actual_status = actual_status
    subscription.is_trial = is_trial
    subscription.is_bio_reward = is_bio  # MagicMock auto-attr иначе truthy
    # +5 минут — буфер против гонки now() фабрики и now() билдера
    subscription.end_date = datetime.now(UTC) + timedelta(days=days_left, hours=hours, minutes=5)

    user = MagicMock()
    user.subscription = subscription
    return user


def test_get_subscription_status_marks_trial_as_trial():
    texts = DummyTexts()
    user = _build_user_with_subscription(actual_status='active', is_trial=True, days_left=5)

    status_text = _get_subscription_status(user, texts)

    assert 'Тестовая подписка' in status_text
    assert 'Активна' not in status_text


def test_get_subscription_status_labels_bio_free_sub():
    texts = DummyTexts()
    user = _build_user_with_subscription(
        actual_status='active', is_trial=True, days_left=2, is_bio=True, hours=23
    )

    status_text = _get_subscription_status(user, texts)

    assert 'Бесплатная подписка' in status_text
    assert 'Тестовая' not in status_text


def test_get_subscription_status_shows_hours_remainder():
    texts = DummyTexts()
    user = _build_user_with_subscription(
        actual_status='active', is_trial=True, days_left=2, hours=23
    )

    status_text = _get_subscription_status(user, texts)

    assert '(2 дн. 23 ч.)' in status_text


def test_get_subscription_status_hides_zero_hours():
    texts = DummyTexts()
    user = _build_user_with_subscription(
        actual_status='active', is_trial=True, days_left=3, hours=0
    )

    status_text = _get_subscription_status(user, texts)

    assert 'дн.' in status_text
    assert 'ч.)' not in status_text


def test_time_left_display_includes_hours():
    from app.database.models import Subscription

    sub = Subscription(end_date=datetime.now(UTC) + timedelta(days=2, hours=23, minutes=30))
    assert sub.time_left_display == '2 дн. 23 ч.'

    sub_flat = Subscription(end_date=datetime.now(UTC) + timedelta(days=3, minutes=30))
    assert sub_flat.time_left_display == '3 дн.'
