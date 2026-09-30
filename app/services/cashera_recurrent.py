"""Чистая логика автопродления через подписки Cashera (без сети и БД).

Как у Platega: сумму и интервал задаём мы при оформлении, Cashera умеет только
daily / weekly / monthly / yearly. Каждое списание приходит вебхуком
``transaction.status_updated`` с объектом ``subscription`` в корне.
"""

from __future__ import annotations

from typing import Any


INTERVAL_DAILY = 'daily'
INTERVAL_WEEKLY = 'weekly'
INTERVAL_MONTHLY = 'monthly'
INTERVAL_YEARLY = 'yearly'

# Локальные статусы записи — общая с Platega/Lava семантика reconciler'а.
LOCAL_ACTIVE_STATUSES = ('PENDING', 'ACTIVE', 'PAST_DUE')

# Статусы транзакции-списания
CHARGE_SUCCESS = {'paid'}
CHARGE_FAILED = {'failed', 'expired'}

# Статус подписки у Cashera -> локальный статус записи
REMOTE_TO_LOCAL: dict[str, str] = {
    'creating': 'PENDING',
    'creation_unknown': 'PENDING',
    'pending_agreement': 'PENDING',
    'active': 'ACTIVE',
    'past_due': 'PAST_DUE',
    'cancelled': 'CANCELLED',
    'failed': 'FAILED',
}


def resolve_cashera_interval(period_days: int, is_daily: bool) -> tuple[str, int]:
    """(interval, charge_days) по периоду тарифа — та же иерархия, что у Platega.

    Неровные периоды приклеиваются к месяцу по 30-дневной цене; ``charge_days``
    задаёт и сумму, и шаг продления.
    """
    if is_daily:
        return INTERVAL_DAILY, 1
    if period_days == 7:
        return INTERVAL_WEEKLY, 7
    if 28 <= period_days <= 31:
        return INTERVAL_MONTHLY, period_days
    if 350 <= period_days <= 380:
        return INTERVAL_YEARLY, period_days
    return INTERVAL_MONTHLY, 30


def round_up_to_rubles(amount_kopeks: int) -> int:
    """Cashera принимает итоговую сумму подписки только в целых рублях (иначе 422).

    Округляем вверх: недобор копеек при каждом списании хуже лишних копеек.
    """
    if amount_kopeks <= 0:
        return 0
    return ((amount_kopeks + 99) // 100) * 100


def build_subscription_external_id(subscription_id: int, nonce: str) -> str:
    """external_id подписки: латиница, цифры, точка, дефис, подчёркивание."""
    return f'casrec{subscription_id}_{nonce}'


def normalize_remote_status(raw: Any) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip().lower()
    return value or None


def local_status_for(remote_status: str | None) -> str | None:
    return REMOTE_TO_LOCAL.get(remote_status or '')


def is_recurring_charge(payload: dict[str, Any]) -> bool:
    """Относится ли вебхук transaction.status_updated к списанию по подписке.

    Признаки из документации: объект ``subscription`` в корне и метод
    ``sbp_recurring``. Любого достаточно.
    """
    if isinstance(payload.get('subscription'), dict):
        return True
    transaction = payload.get('transaction')
    return isinstance(transaction, dict) and transaction.get('payment_method') == 'sbp_recurring'


def cashera_reconcile_decision(
    local_status: str,
    remote_status: str | None,
    age_minutes: float,
    *,
    remote_missing: bool = True,
) -> str | None:
    """Новый локальный статус по данным Cashera, либо None — не трогать.

    ``remote_missing``: True — Cashera достоверно не знает подписку (нет uuid /
    404), False — транспортный сбой, зависший PENDING хоронить рано. Первое
    сработавшее правило выигрывает (зеркало Platega/Lava).
    """
    if remote_status == 'active' and local_status in ('PENDING', 'PAST_DUE'):
        return 'ACTIVE'
    if remote_status == 'cancelled' and local_status != 'CANCELLED':
        return 'CANCELLED'
    if remote_status == 'failed' and local_status not in ('FAILED', 'CANCELLED'):
        return 'FAILED'
    if remote_status == 'past_due' and local_status not in ('PAST_DUE', 'CANCELLED'):
        return 'PAST_DUE'
    # pending_agreement: клиент не подтвердил ссылку. Живую запись дольше суток не
    # держим — иначе partial unique навсегда закрывает повторное оформление.
    if remote_status == 'pending_agreement' and local_status == 'PENDING' and age_minutes > 24 * 60:
        return 'FAILED'
    if remote_status is None and remote_missing and local_status == 'PENDING' and age_minutes > 30:
        return 'FAILED'
    return None
