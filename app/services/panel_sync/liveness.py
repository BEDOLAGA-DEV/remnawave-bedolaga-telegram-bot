"""Единственное определение: должна ли подписка быть включена в панели.

Правило существовало в пяти копиях трёх разных редакций. Две из них не проверяли
статус пользователя, и любая синхронизация «из бота в панель» возвращала
заблокированному доступ: в панель уходил ACTIVE, потому что строка подписки ещё
была активной. Возвращал блокировку только фоновый мониторинг — до следующего
нажатия кнопки.

Про ``Subscription.actual_status``: свойство модели считает то же самое, но берёт
время само, поэтому в тестах его не подменить, а синхронизации нужен внешний
момент времени. Здесь повторено его правило с явными часами; совпадение
закреплено тестом ``test_matches_actual_status_of_the_model``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.database.models import SubscriptionStatus, UserStatus


#: Колонки статуса, при которых подписка вообще может считаться живой.
_LIVE_STATUSES = frozenset({SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value})


def is_subscription_live(user, subscription, *, now: datetime | None = None) -> bool:
    """Включать ли пользователя в панели ради этой подписки."""
    moment = now or datetime.now(UTC)

    if getattr(user, 'status', UserStatus.ACTIVE.value) != UserStatus.ACTIVE.value:
        return False

    if getattr(subscription, 'status', None) not in _LIVE_STATUSES:
        return False

    end_date = getattr(subscription, 'end_date', None)
    if end_date is None:
        return False
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=UTC)
    return end_date > moment
