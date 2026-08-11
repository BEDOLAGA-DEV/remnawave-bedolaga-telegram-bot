"""Чистая логика рекуррентных СБП-подписок Platega (без сети и БД)."""

from __future__ import annotations


# Platega paymentMethod для подписки
PLATEGA_SUBSCRIPTION_METHOD = 6

# interval: 1=day, 2=week, 3=month, 4=year
INTERVAL_DAY = 1
INTERVAL_WEEK = 2
INTERVAL_MONTH = 3
INTERVAL_YEAR = 4

# Статусы коллбеков
CHARGE_SUCCESS = {'CONFIRMED'}
# Провальный чардж: докам известен CANCELED, но словарь разовых платежей Platega
# в этом же проекте включает FAILED/EXPIRED — неизвестный провальный статус
# уронил бы переход в PAST_DUE (ни счётчика, ни уведомления юзеру).
CHARGE_FAILED = {'CANCELED', 'FAILED', 'EXPIRED'}
SUB_ACTIVATED = 'SUBSCRIPTION_ACTIVATED'
SUB_PAST_DUE = 'SUBSCRIPTION_PAST_DUE'
SUB_CANCELLED = 'SUBSCRIPTION_CANCELLED'
SUB_FAILED = 'SUBSCRIPTION_FAILED'


def resolve_platega_interval(period_days: int, is_daily: bool) -> tuple[int, int]:
    """Возвращает (interval, charge_days) для подписки Platega.

    Platega умеет только day/week/month/year (count=1). Каденс выводится из
    числа дней тарифа; неровные периоды приклеиваются к месяцу по 30-дневной
    цене (см. спеку §3). charge_days задаёт и сумму, и шаг продления.
    """
    if is_daily:
        return INTERVAL_DAY, 1
    if period_days == 7:
        return INTERVAL_WEEK, 7
    if 28 <= period_days <= 31:
        return INTERVAL_MONTH, period_days
    if 350 <= period_days <= 380:
        return INTERVAL_YEAR, period_days
    return INTERVAL_MONTH, 30


def platega_reconcile_decision(
    local_status: str,
    remote_status: str | None,
    age_minutes: float,
    *,
    remote_missing: bool = True,
) -> str | None:
    """New local status given the Platega-reported status, or None for no change.

    remote_status is normalized lowercase (Platega get-subscription `status`), or
    None when Platega has no record / the lookup failed. ``remote_missing``
    disambiguates the None case: True — провайдер ДОСТОВЕРНО не знает такой
    подписки (HTTP 404, либо у записи вовсе нет platega_subscription_id);
    False — Platega недоступна (транспортный сбой) и хоронить зависший PENDING
    рано: решение откладывается до следующего цикла. Used by the monitoring
    reconciler (safety net for lost callbacks / stuck PENDING records) — first
    matching rule wins.
    """
    if remote_status == 'active' and local_status in ('PENDING', 'PAST_DUE'):
        return 'ACTIVE'
    if remote_status in ('cancelled', 'canceled') and local_status != 'CANCELLED':
        return 'CANCELLED'
    if remote_status == 'failed' and local_status != 'FAILED':
        return 'FAILED'
    if remote_status in ('pastdue', 'past_due', 'past due') and local_status not in ('PAST_DUE', 'CANCELLED'):
        return 'PAST_DUE'
    if remote_status is None and remote_missing and local_status == 'PENDING' and age_minutes > 30:
        return 'FAILED'
    return None
