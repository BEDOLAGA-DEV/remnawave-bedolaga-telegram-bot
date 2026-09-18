from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from app.handlers.menu import _get_subscription_status
from tests.fixtures.local_day import reset_local_timezone_cache, use_timezone  # noqa: F401


class DummyTexts:
    def t(self, key: str, default: str):  # pragma: no cover - simple stub
        return default


def _build_user_with_subscription(actual_status: str, is_trial: bool, days_left: int):
    subscription = MagicMock()
    subscription.actual_status = actual_status
    subscription.is_trial = is_trial
    subscription.end_date = datetime.now(UTC) + timedelta(days=days_left, hours=1)

    user = MagicMock()
    user.subscription = subscription
    return user


def test_get_subscription_status_marks_trial_as_trial():
    texts = DummyTexts()
    user = _build_user_with_subscription(actual_status='active', is_trial=True, days_left=5)

    status_text = _get_subscription_status(user, texts)

    assert 'Тестовая подписка' in status_text
    assert 'Активна' not in status_text


# Жалоба 4.12.0: 16.09 14:21 МСК, подписка до 18.09 14:17 МСК — меню писало
# «истекает завтра». Неполные сутки отбрасывались, единица читалась как «завтра».
_COMPLAINT_NOW = datetime(2026, 9, 16, 11, 21, tzinfo=UTC)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _COMPLAINT_NOW if tz is not None else _COMPLAINT_NOW.replace(tzinfo=None)


@pytest.fixture
def complaint_clock(monkeypatch, reset_local_timezone_cache):
    use_timezone(monkeypatch, 'Europe/Moscow')
    monkeypatch.setattr('app.handlers.menu.datetime', _FrozenDatetime)


def _user_ending_at(end_date: datetime, *, is_trial: bool = False):
    subscription = MagicMock()
    subscription.actual_status = 'active'
    subscription.is_trial = is_trial
    subscription.end_date = end_date
    user = MagicMock()
    user.subscription = subscription
    return user


@pytest.mark.usefixtures('complaint_clock')
@pytest.mark.parametrize('is_trial', [False, True])
def test_almost_two_days_left_is_not_tomorrow(is_trial):
    user = _user_ending_at(datetime(2026, 9, 18, 11, 17, tzinfo=UTC), is_trial=is_trial)

    status_text = _get_subscription_status(user, DummyTexts())

    assert 'завтра' not in status_text
    assert '2 дн.' in status_text


@pytest.mark.usefixtures('complaint_clock')
def test_two_hours_past_local_midnight_is_tomorrow():
    # Сейчас 16.09 14:21 МСК, конец 17.09 01:00 МСК — меньше суток, но уже завтра.
    user = _user_ending_at(datetime(2026, 9, 16, 22, 0, tzinfo=UTC))

    assert 'истекает завтра' in _get_subscription_status(user, DummyTexts())


@pytest.mark.usefixtures('complaint_clock')
def test_later_today_is_today():
    user = _user_ending_at(datetime(2026, 9, 16, 20, 0, tzinfo=UTC))  # 23:00 МСК

    assert 'истекает сегодня' in _get_subscription_status(user, DummyTexts())
