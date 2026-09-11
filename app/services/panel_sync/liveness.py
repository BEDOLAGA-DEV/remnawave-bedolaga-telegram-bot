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
    """Включать ли пользователя в панели ради этой подписки.

    ``user=None`` означает «владелец неизвестен»: проверяем только подписку.
    Так зовут те немногие места, где объект пользователя не загружен, а
    подтягивать его ради одной проверки дороже, чем польза, — блокировку там
    всё равно донесёт статус, собранный писателем.
    """
    moment = now or datetime.now(UTC)

    if user is not None and getattr(user, 'status', UserStatus.ACTIVE.value) != UserStatus.ACTIVE.value:
        return False

    if getattr(subscription, 'status', None) not in _LIVE_STATUSES:
        return False

    end_date = getattr(subscription, 'end_date', None)
    if end_date is None:
        return False
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=UTC)
    return end_date > moment


def is_subscription_expired(subscription, *, now: datetime | None = None) -> bool:
    """Истекла ли подписка по дате — состояние, которое панель выводит сама.

    Совпадает с ``Subscription.actual_status == 'expired'``: колонка EXPIRED,
    либо ACTIVE/TRIAL с прошедшей (или отсутствующей) датой. DISABLED и LIMITED
    сюда не входят — это другие состояния со своими правилами. Правило нужно
    писателю (истёкшей подписке статус в панель не шлём — там нет «истекла»,
    только «отключена админом»), вебхуку и импорту (DISABLED из панели у уже
    истёкшей подписки ничего не меняет и переноситься не должен).
    """
    status = getattr(subscription, 'status', None)
    if status == SubscriptionStatus.EXPIRED.value:
        return True
    if status not in _LIVE_STATUSES:
        return False
    end_date = getattr(subscription, 'end_date', None)
    if end_date is None:
        return True
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=UTC)
    return end_date <= (now or datetime.now(UTC))
