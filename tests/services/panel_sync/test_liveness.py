"""«Включить ли подписку в панели» — одно определение на всех писателей.

Раньше правило было скопировано в пять мест в трёх редакциях. Две из них не
смотрели на статус пользователя, поэтому массовая синхронизация «Из бота в
панель» и кнопки кабинета отправляли заблокированному пользователю ACTIVE —
то есть возвращали ему доступ до следующего прохода мониторинга.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.database.models import SubscriptionStatus
from app.services.panel_sync import is_subscription_live


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _sub(status=SubscriptionStatus.ACTIVE.value, days=30):
    return SimpleNamespace(status=status, end_date=NOW + timedelta(days=days))


def _user(status='active'):
    return SimpleNamespace(status=status)


def test_active_subscription_of_an_active_user_is_live():
    assert is_subscription_live(_user(), _sub(), now=NOW) is True


def test_trial_counts_as_live():
    assert is_subscription_live(_user(), _sub(status=SubscriptionStatus.TRIAL.value), now=NOW) is True


def test_blocked_user_is_never_live():
    """Блокировка обязана доезжать до панели: иначе синхронизация снимает бан."""
    assert is_subscription_live(_user('blocked'), _sub(), now=NOW) is False


def test_deleted_user_is_never_live():
    assert is_subscription_live(_user('deleted'), _sub(), now=NOW) is False


def test_expired_by_date_is_not_live_even_with_active_column():
    assert is_subscription_live(_user(), _sub(days=-1), now=NOW) is False


def test_disabled_and_limited_columns_are_not_live():
    assert is_subscription_live(_user(), _sub(status=SubscriptionStatus.DISABLED.value), now=NOW) is False
    assert is_subscription_live(_user(), _sub(status=SubscriptionStatus.LIMITED.value), now=NOW) is False


def test_missing_end_date_is_not_live():
    sub = _sub()
    sub.end_date = None

    assert is_subscription_live(_user(), sub, now=NOW) is False


def test_naive_end_date_is_read_as_utc():
    """В базах до TIMESTAMPTZ дата приходит наивной — считаем её UTC, а не локальной."""
    sub = _sub()
    sub.end_date = (NOW + timedelta(days=1)).replace(tzinfo=None)

    assert is_subscription_live(_user(), sub, now=NOW) is True


def test_user_without_status_attribute_is_treated_as_active():
    """Часть вызывающих отдаёт облегчённый объект без статуса — синк не роняем."""
    assert is_subscription_live(SimpleNamespace(), _sub(), now=NOW) is True


def test_matches_actual_status_of_the_model():
    """Правило обязано совпадать с Subscription.actual_status — иначе разъедутся снова."""
    from app.database.models import Subscription

    for column_status in (
        SubscriptionStatus.ACTIVE.value,
        SubscriptionStatus.TRIAL.value,
        SubscriptionStatus.EXPIRED.value,
        SubscriptionStatus.DISABLED.value,
        SubscriptionStatus.LIMITED.value,
    ):
        for days in (30, -1):
            subscription = Subscription(status=column_status, end_date=datetime.now(UTC) + timedelta(days=days))
            model_says_live = subscription.actual_status in ('active', 'trial')

            assert is_subscription_live(_user(), subscription) is model_says_live, (
                f'{column_status}, дней={days}: правило разошлось с actual_status'
            )
