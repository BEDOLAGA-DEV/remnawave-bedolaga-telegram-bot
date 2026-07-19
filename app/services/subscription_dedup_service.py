"""One-shot cleanup of duplicate multi-tariff subscriptions (DB rows only).

Re-buying a tariff after it expired used to create a NEW subscription instead of
reviving the old one, so users piled up stacks of expired same-tariff
duplicates. The purchase path now revives in place (``create_paid_subscription``);
this collapses the duplicate DB rows that already accumulated.

Scope is deliberately DB-only and conservative:

* Per (user, tariff) it keeps one survivor — most "alive" first
  (active > limited > trial > expired), then the latest ``end_date`` — and
  deletes only the redundant EXPIRED/DISABLED rows. Live subscriptions
  (active / limited / trial), lone rows and pending are never touched, so the
  survivor's panel user is never at risk.
* It does NOT delete Remnawave panel users. Each multi-tariff subscription has
  its own panel user, but panel deletion is a non-transactional API call: doing
  it here risked removing a user that is still referenced (A063 "user not found")
  if the surrounding DB transaction didn't commit. The expired duplicate panel
  users are inactive and harmless; if a live subscription's panel user is ever
  missing, the normal sync recreates it (see
  ``SubscriptionService._create_or_update_remnawave_user_multi``).

Runs once in the background on startup. Idempotent — a no-op once there are no
duplicates.
"""

import structlog
from sqlalchemy import select

from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, SubscriptionStatus


logger = structlog.get_logger(__name__)

# Lower rank = better survivor. Statuses not listed (e.g. disabled/pending) sort last.
_SURVIVOR_PRIORITY = {
    SubscriptionStatus.ACTIVE.value: 0,
    SubscriptionStatus.LIMITED.value: 1,
    SubscriptionStatus.TRIAL.value: 2,
    SubscriptionStatus.EXPIRED.value: 3,
}
_REMOVABLE_STATUSES = frozenset({SubscriptionStatus.EXPIRED.value, SubscriptionStatus.DISABLED.value})


def _survivor_key(sub: Subscription) -> tuple[int, float]:
    end_ts = sub.end_date.timestamp() if sub.end_date else 0.0
    return (_SURVIVOR_PRIORITY.get(sub.status, 4), -end_ts)


async def _run_dedupe() -> dict[str, int]:
    removed_db = 0

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Subscription).where(
                Subscription.tariff_id.isnot(None),
                Subscription.is_trial.is_(False),
            )
        )
        groups: dict[tuple[int, int], list[Subscription]] = {}
        for sub in result.scalars().all():
            groups.setdefault((sub.user_id, sub.tariff_id), []).append(sub)

        for subs in groups.values():
            if len(subs) < 2:
                continue
            subs.sort(key=_survivor_key)
            _survivor, *rest = subs
            for dup in rest:
                if dup.status not in _REMOVABLE_STATUSES:
                    continue  # never remove a live subscription
                await db.delete(dup)
                removed_db += 1

        if removed_db:
            await db.commit()

    if removed_db:
        logger.info('🧹 Схлопнуты дубли тарифных подписок', removed_db=removed_db)
    return {'removed_db': removed_db}


async def dedupe_expired_tariff_subscriptions() -> dict[str, int]:
    """Background-safe entrypoint: never raises, returns the count removed."""
    try:
        return await _run_dedupe()
    except Exception as error:
        logger.error('dedup: cleanup pass failed', error=error)
        return {'removed_db': 0}
