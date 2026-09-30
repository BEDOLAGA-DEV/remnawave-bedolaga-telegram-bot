"""Валидация периода автопродления — общая для balance-autopay и СБП-рекуррентов.

Живёт отдельно от monitoring_service: платёжные модули (Platega, Cashera) зовут её при
оформлении, и импорт мониторинга оттуда замыкал кольцо
crud.subscription → payment.* → monitoring_service (CodeQL py/cyclic-import).
"""

from __future__ import annotations

from app.config import settings


def resolve_autopay_period_candidate(candidate, tariff) -> int | None:
    """Return ``candidate`` only if it is a valid renewal period for ``tariff``.

    Validation is **fail-closed**: we never let an unvalidated period drive
    autopay extension. Resolution order for the allowlist:

    1. ``tariff.get_available_periods()`` if the tariff exists and has any
       priced periods.
    2. ``settings.get_available_renewal_periods()`` as the global allowlist
       (for tariff-less / classic-mode subscriptions, or tariffs with empty
       ``period_prices``).

    Returns ``None`` for ``candidate`` that is falsy, non-positive, or not in
    either allowlist — letting the caller fall through to the next tier
    (typically ``tariff.get_shortest_period()`` and finally the hard 30-day
    floor).
    """
    if not candidate or candidate <= 0:
        return None

    available_periods: list[int] = []
    if tariff is not None:
        try:
            available_periods = list(tariff.get_available_periods() or [])
        except Exception:
            available_periods = []

    if not available_periods:
        try:
            available_periods = list(settings.get_available_renewal_periods() or [])
        except Exception:
            available_periods = []

    if not available_periods or candidate not in available_periods:
        return None
    return candidate
