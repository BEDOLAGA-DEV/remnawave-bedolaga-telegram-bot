"""Чистая логика рекуррентных СБП-подписок Platega (без сети и БД)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CallbackFields:
    """Поля коллбека Platega, приведённые к одному виду."""

    status: str | None
    subscription_id: str | None
    charge_id: str | None
    next_charge_at: Any


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
    числа дней тарифа; charge_days задаёт и сумму, и шаг продления.
    """
    if is_daily or period_days <= 1:
        return INTERVAL_DAY, 1
    if 6 <= period_days <= 14:
        return INTERVAL_WEEK, period_days
    if 28 <= period_days <= 31:
        return INTERVAL_MONTH, period_days
    if 350 <= period_days <= 380:
        return INTERVAL_YEAR, period_days
    return INTERVAL_MONTH, period_days


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


# --- разбор коллбеков ------------------------------------------------------
#
# Platega во всём остальном использует camelCase: исходящее создание подписки
# шлёт `paymentMethod`/`amount`/`interval`, разовый коллбек читается как `id` и
# `status`. Подписочная ветка изначально писалась под PascalCase из примеров в
# спеке, и на живом мерчанте коллбек списания (`{"id": ..., "status":
# "CONFIRMED", ...}`) не совпадал ни с одним условием маршрутизации: он уходил
# в обработчик разовых платежей, тот не находил локальный платёж (под
# рекуррентное списание записи в payments нет вовсе) и отвечал 400 —
# «Callback delivery failed» на стороне Platega, автопродление не работало.
#
# Поэтому имена полей сверяем без учёта регистра и не полагаемся на конкретный
# вариант написания.


def _lower_keys(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Копия пейлоада с ключами в нижнем регистре.

    При коллизии (`Id` и `id` рядом) побеждает первое непустое значение —
    пустышка не должна затирать реальный идентификатор.
    """
    lowered: dict[str, Any] = {}
    for key, value in payload.items():
        name = str(key).lower()
        if name in lowered and lowered[name] not in (None, ''):
            continue
        lowered[name] = value
    return lowered


def _text(value: Any) -> str | None:
    """Непустая строка либо None (числовые id Platega приводим к строке)."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def is_subscription_callback(payload: Mapping[str, Any]) -> bool:
    """Относится ли коллбек к рекуррентной СБП-подписке.

    Три независимых признака: метод оплаты подписки, идентификатор подписки в
    теле и статус самой подписки. Любого достаточно — Platega шлёт списания и
    смены статуса подписки на тот же путь, что и разовые платежи.
    """
    fields = _lower_keys(payload)

    method = _text(fields.get('paymentmethod'))
    if method is not None and method == str(PLATEGA_SUBSCRIPTION_METHOD):
        return True

    if 'subscriptionid' in fields:
        return True

    status = _text(fields.get('status')) or ''
    return status.upper().startswith('SUBSCRIPTION_')


def read_callback_fields(payload: Mapping[str, Any]) -> CallbackFields:
    """Поля подписочного коллбека, независимо от регистра ключей."""
    fields = _lower_keys(payload)
    return CallbackFields(
        status=_text(fields.get('status')),
        subscription_id=_text(fields.get('subscriptionid')),
        charge_id=_text(fields.get('id')),
        next_charge_at=fields.get('nextchargeat'),
    )
