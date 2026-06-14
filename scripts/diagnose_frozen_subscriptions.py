#!/usr/bin/env python3
"""Read-only: list subscriptions that carry a freeze marker (frozen_at set).

Background
----------
A user-initiated freeze sets ``subscription.frozen_at`` and disables the paired
RemnaWave panel account, while the DB row is meant to stay ACTIVE. A bug let the
panel's ``user.disabled`` webhook echo flip the DB status to DISABLED (see the
frozen guard in ``_handle_user_disabled``). Such rows are STUCK: the
"Разморозить" button used to be hidden for non-active subscriptions, so the user
could not unfreeze.

This tool only SELECTs. It changes nothing. It reports every frozen row grouped
by status so you can see how many are healthy (active) vs. stuck (disabled /
limited / expired).

Usage (inside the bot container)
--------------------------------
    docker compose exec bot python scripts/diagnose_frozen_subscriptions.py
        -> dump every subscription with frozen_at set, grouped by status.

    docker compose exec bot python scripts/diagnose_frozen_subscriptions.py --telegram-id 5178677268
        -> limit to specific telegram_id(s); repeatable.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from pathlib import Path

# Make repo root importable when running as `python scripts/diagnose_frozen_subscriptions.py`.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from app.database.database import AsyncSessionLocal  # noqa: E402
from app.database.models import Subscription, SubscriptionStatus  # noqa: E402


# Frozen rows in these statuses are "stuck": the freeze marker is set but the
# status is not ACTIVE, so the freeze/resume flow is in a desynced state.
STUCK_STATUSES = {
    SubscriptionStatus.DISABLED.value,
    SubscriptionStatus.LIMITED.value,
    SubscriptionStatus.EXPIRED.value,
}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description='Read-only report of subscriptions with frozen_at set.')
    p.add_argument(
        '--telegram-id', type=int, action='append', default=None,
        help='Limit to specific telegram_id(s). Repeatable. Default: all users.',
    )
    return p.parse_args()


async def _load_frozen(db, telegram_ids: list[int] | None) -> list[Subscription]:
    stmt = (
        select(Subscription)
        .options(selectinload(Subscription.user))
        .where(Subscription.frozen_at.isnot(None))
        .order_by(Subscription.status, Subscription.id)
    )
    result = await db.execute(stmt)
    subs = list(result.scalars().all())
    if telegram_ids:
        wanted = {int(t) for t in telegram_ids}
        subs = [s for s in subs if s.user and s.user.telegram_id in wanted]
    return subs


def _fmt(sub: Subscription) -> str:
    tg = getattr(sub.user, 'telegram_id', None) if sub.user else None
    return (
        f'sub {sub.id} (tg {tg}): status={sub.status} '
        f'frozen_at={sub.frozen_at} frozen_until={sub.frozen_until} '
        f'end_date={sub.end_date} uuid={sub.remnawave_uuid}'
    )


async def main() -> None:
    args = _parse_args()
    async with AsyncSessionLocal() as db:
        subs = await _load_frozen(db, args.telegram_id)

    by_status: dict[str, list[Subscription]] = defaultdict(list)
    for s in subs:
        by_status[s.status].append(s)

    total = len(subs)
    stuck = [s for s in subs if s.status in STUCK_STATUSES]

    print(f'Frozen subscriptions (frozen_at set): {total} total')
    for status in sorted(by_status):
        rows = by_status[status]
        tag = ' <-- STUCK' if status in STUCK_STATUSES else ' (healthy frozen)'
        print(f'\n  status={status}: {len(rows)}{tag}')
        for s in rows:
            print(f'    {_fmt(s)}')

    print(f'\nSTUCK total (frozen but not active): {len(stuck)}')
    if stuck:
        print('These rows are frozen yet non-active. With the new resume-button '
              'fallback the user can press "Разморозить" to self-heal via the '
              'enable webhook; if webhooks are unreliable, remediate explicitly.')


if __name__ == '__main__':
    asyncio.run(main())
