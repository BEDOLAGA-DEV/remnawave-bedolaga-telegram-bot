"""Чистая логика рекуррентных подписок Lava (без сети и БД).

Отличие от Platega: у Lava нельзя подписать на произвольную сумму с произвольным
интервалом — подписка оформляется на ПРОДУКТ, созданный в кабинете Lava, и цена
с периодичностью заданы там (``recurrent/product/list``). Поэтому шаг продления
(``charge_days``) берётся из продукта, а не выводится из тарифа.
"""

from __future__ import annotations


# Префикс orderId рекуррентных подписок: по нему вебхук отличает списание по
# подписке от обычного инвойса пополнения баланса (тот же /lava-webhook).
LAVA_RECURRENT_ORDER_PREFIX = 'lavarec'

# Локальные статусы записи (те же, что у Platega — общая семантика reconciler'а)
LOCAL_ACTIVE_STATUSES = ('PENDING', 'ACTIVE', 'PAST_DUE')

# Статусы вебхука Lava по инвойсу списания (те же, что у обычных платежей)
CHARGE_SUCCESS = {'success'}
CHARGE_FAILED = {'cancel', 'cancelled', 'error', 'failed', 'expired'}

# period enum продукта Lava -> количество дней в периоде.
# periodDays продукт отдаёт сам, но при его отсутствии считаем по enum.
PRODUCT_PERIOD_DAYS: dict[str, int] = {
    'ten_minute': 1,
    'thirty_minute': 1,
    'one_hour': 1,
    'two_hours': 1,
    'one_day': 1,
    'two_days': 2,
    'three_days': 3,
    'one_month': 30,
    'three_months': 90,
    'six_months': 180,
    'year': 365,
}


def build_recurrent_order_id(subscription_id: int, nonce: str) -> str:
    """orderId подписки: префикс + id локальной подписки + случайный хвост."""
    return f'{LAVA_RECURRENT_ORDER_PREFIX}{subscription_id}_{nonce}'


def is_recurrent_order_id(order_id: str | None) -> bool:
    return bool(order_id) and str(order_id).startswith(LAVA_RECURRENT_ORDER_PREFIX)


def resolve_product_charge_days(product: dict | None) -> int:
    """Шаг продления по данным продукта Lava.

    ``periodDays`` — источник истины; enum ``period`` используется как фолбэк.
    Внутрисуточные периоды (часы/минуты) схлопываются в 1 день: подписка бота
    меряется днями, продлевать на 0 дней нельзя.
    """
    if not product:
        return 30
    raw_days = product.get('periodDays')
    try:
        days = int(raw_days) if raw_days is not None else 0
    except (TypeError, ValueError):
        days = 0
    if days > 0:
        return days
    return PRODUCT_PERIOD_DAYS.get(str(product.get('period') or '').strip().lower(), 30)


def normalize_remote_status(raw: str | None) -> str | None:
    """Приводит статус подписки из ``subscription/status`` к нормализованному виду.

    Lava не документирует перечень значений, поэтому маппинг защитный: любое
    неизвестное значение возвращается как есть в нижнем регистре, а reconciler
    на него просто не реагирует (лучше бездействие, чем ложный перевод статуса).
    """
    if raw is None:
        return None
    value = str(raw).strip().lower().replace('-', '_').replace(' ', '_')
    if not value:
        return None
    # 'activated'/'deactivated' — фактические значения Lava (примеры в спеке
    # SubscriptionStatusResource; у деактивированной подписки есть ещё
    # deactivation_time/deactivated_reason). Без них reconciler был no-op.
    if value in ('active', 'activated', 'subscribed', 'paid'):
        return 'active'
    if value in ('cancelled', 'canceled', 'deactivated', 'unsubscribed', 'stopped'):
        return 'cancelled'
    if value in ('failed', 'error', 'declined'):
        return 'failed'
    if value in ('past_due', 'pastdue', 'overdue', 'unpaid'):
        return 'past_due'
    if value in ('pending', 'created', 'waiting', 'new'):
        return 'pending'
    if value in ('expired', 'finished', 'completed'):
        return 'expired'
    return value


def lava_reconcile_decision(
    local_status: str,
    remote_status: str | None,
    age_minutes: float,
    *,
    remote_missing: bool = True,
) -> str | None:
    """Новый локальный статус по данным Lava, либо None — не трогать.

    ``remote_status`` уже нормализован (:func:`normalize_remote_status`), None —
    Lava не знает такой подписки ИЛИ запрос не удался. ``remote_missing``
    разделяет эти случаи: True — провайдер достоверно не знает подписку (нет
    remote-id / 404), False — транспортный сбой, и хоронить зависший PENDING
    рано, решение откладывается до следующего цикла. Зеркало
    ``platega_reconcile_decision``: первое сработавшее правило выигрывает.
    """
    if remote_status == 'active' and local_status in ('PENDING', 'PAST_DUE'):
        return 'ACTIVE'
    if remote_status == 'cancelled' and local_status != 'CANCELLED':
        return 'CANCELLED'
    if remote_status in ('failed', 'expired') and local_status not in ('FAILED', 'CANCELLED'):
        return 'FAILED'
    if remote_status == 'past_due' and local_status not in ('PAST_DUE', 'CANCELLED'):
        return 'PAST_DUE'
    if remote_status is None and remote_missing and local_status == 'PENDING' and age_minutes > 30:
        return 'FAILED'
    return None
