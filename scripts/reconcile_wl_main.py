#!/usr/bin/env python3
"""Reconcile subscriptions mis-bound to a DUPLICATE RemnaWave main account.

Background
---------
A bug in the multi-tariff create path adopted a pre-existing legacy panel
account only under the hardcoded name ``user_<telegram_id>``. Deployments whose
``REMNAWAVE_USER_USERNAME_TEMPLATE`` is customised (e.g. ``u_{telegram_id}``)
never matched their real ``u_<tg>`` account, so the bot created a DUPLICATE
``u_<tg>_<short>`` main account + ``<...>_wl`` and bound the subscription to it.
The user's real in-VPN ``u_<tg>`` / ``u_<tg>_wl`` pair was then orphaned and
never renewed, so the user silently lost access on renew/extend.

The code fix (the legacy fallback now also tries the current template base) stops
NEW corruption, but it cannot self-heal subscriptions ALREADY bound to a
duplicate: their ``subscription.remnawave_uuid`` is set, so the create path's
UUID branch short-circuits before the adoption fallback runs.

This one-shot reconciler finds those victims and repoints them back to the real
legacy account, refreshes it (which also rebuilds the correct ``u_<tg>_wl``), and
DISABLES (not deletes) the duplicate so a bad repoint stays recoverable.

Usage (inside the bot container)
--------------------------------
    docker compose exec bot python scripts/reconcile_wl_main.py
        -> DRY-RUN: report victims only, change nothing.

    docker compose exec bot python scripts/reconcile_wl_main.py --apply
        -> repoint victims + disable duplicates.

    docker compose exec bot python scripts/reconcile_wl_main.py --apply \
        --telegram-id 2032872553
        -> only that user (repeatable; pass several --telegram-id).

    docker compose exec bot python scripts/reconcile_wl_main.py --apply \
        --delete-duplicates
        -> delete the duplicate main/_wl instead of disabling (use only after a
           soak period — disable is the safe default).

Safety
------
- DRY-RUN by default; ``--apply`` is required to change anything.
- telegram_id users only — email-only username renders can collide/degenerate.
- Ownership guard: the legacy panel account's telegram_id must match the user's.
- Idempotent: a subscription already pointing at the legacy account is skipped,
  so the script is safe to re-run.
- One legacy account is claimed by at most one subscription per run; extra
  subscriptions mapping to the same legacy account are reported as AMBIGUOUS and
  left untouched for manual review.
- Duplicates are DISABLED (recoverable) unless ``--delete-duplicates``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Make repo root importable when running as `python scripts/reconcile_wl_main.py`.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from app.config import settings  # noqa: E402
from app.database.database import AsyncSessionLocal  # noqa: E402
from app.database.models import Subscription, SubscriptionStatus  # noqa: E402
from app.services.subscription_service import SubscriptionService  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description='Reconcile subscriptions bound to a duplicate RemnaWave main account.')
    p.add_argument('--apply', action='store_true', help='Apply changes (default: dry-run, report only).')
    p.add_argument(
        '--telegram-id', type=int, action='append', default=None,
        help='Limit to specific telegram_id(s). Repeatable. Default: all users.',
    )
    p.add_argument(
        '--delete-duplicates', action='store_true',
        help='Delete the duplicate main/_wl accounts instead of disabling them (use only after a soak period).',
    )
    return p.parse_args()


async def _load_candidates(db, telegram_ids: list[int] | None) -> list[Subscription]:
    """Active/trial subscriptions that have a panel main account, with user+tariff loaded."""
    stmt = (
        select(Subscription)
        .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
        .where(
            Subscription.status.in_([SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value]),
            Subscription.remnawave_uuid.isnot(None),
        )
        .order_by(Subscription.id)
    )
    result = await db.execute(stmt)
    subs = list(result.scalars().all())
    if telegram_ids:
        wanted = {int(t) for t in telegram_ids}
        subs = [s for s in subs if s.user and s.user.telegram_id in wanted]
    return subs


async def main() -> int:
    args = _parse_args()
    mode = 'APPLY' if args.apply else 'DRY-RUN'
    dup_action = 'DELETE' if args.delete_duplicates else 'DISABLE'

    if not settings.is_multi_tariff_enabled():
        print('Multi-tariff mode is disabled — this bug class does not apply. Nothing to do.')
        return 0

    print(f'=== reconcile_wl_main [{mode}] — duplicates will be {dup_action}d ===\n')

    svc = SubscriptionService()
    victims = 0
    healed = 0
    ambiguous = 0
    skipped_owner = 0
    claimed_legacy: set[str] = set()  # legacy uuids already repointed this run

    async with AsyncSessionLocal() as db:
        subs = await _load_candidates(db, args.telegram_id)
        print(f'Scanning {len(subs)} active/trial subscription(s) with a panel main account...\n')

        async with svc.get_api_client() as api:
            for sub in subs:
                user = sub.user
                if not user or not user.telegram_id:
                    continue  # telegram_id users only

                try:
                    legacy_name = settings.format_remnawave_username(
                        full_name=user.full_name,
                        username=user.username,
                        telegram_id=user.telegram_id,
                        email=user.email,
                        user_id=user.id,
                    )
                except Exception as e:  # pragma: no cover - defensive
                    print(f'  sub {sub.id} (tg {user.telegram_id}): could not build legacy name: {e}')
                    continue

                try:
                    legacy_user = await api.get_user_by_username(legacy_name)
                except Exception as e:
                    print(f'  sub {sub.id} (tg {user.telegram_id}): legacy lookup {legacy_name!r} failed: {e}')
                    continue
                if not legacy_user or not getattr(legacy_user, 'uuid', None):
                    continue  # no legacy account — nothing to reconcile

                if legacy_user.uuid == sub.remnawave_uuid:
                    continue  # already correctly bound — idempotent skip

                # Ownership guard.
                ltg = getattr(legacy_user, 'telegram_id', None)
                if ltg is not None and str(ltg) != str(user.telegram_id):
                    skipped_owner += 1
                    print(
                        f'  sub {sub.id} (tg {user.telegram_id}): legacy {legacy_name!r} belongs to tg {ltg} '
                        f'— SKIP (ownership mismatch).'
                    )
                    continue

                victims += 1
                print(
                    f'  VICTIM sub {sub.id} (tg {user.telegram_id}):\n'
                    f'      bound to duplicate uuid {sub.remnawave_uuid}\n'
                    f'      real legacy account  {legacy_name!r} -> uuid {legacy_user.uuid}'
                )

                if legacy_user.uuid in claimed_legacy:
                    ambiguous += 1
                    print('      AMBIGUOUS: another subscription already claimed this legacy account — manual review.')
                    continue

                if not args.apply:
                    continue

                # --- APPLY ---
                dup_main_uuid = sub.remnawave_uuid
                # Resolve the duplicate's paired _wl BEFORE we repoint/refresh,
                # while the duplicate main username is still resolvable.
                dup_wl_uuid = await svc._resolve_paired_wl_uuid(api, dup_main_uuid)

                # Repoint to the legacy account and refresh it. update_remnawave_user
                # rebuilds the correct '<legacy>_wl', persists short_uuid /
                # subscription_url / crypto_link, and commits.
                sub.remnawave_uuid = legacy_user.uuid
                claimed_legacy.add(legacy_user.uuid)
                try:
                    await svc.update_remnawave_user(db, sub, sync_squads=True)
                except Exception as e:
                    print(f'      ERROR refreshing legacy account, rolling back: {e}')
                    await db.rollback()
                    claimed_legacy.discard(legacy_user.uuid)
                    continue

                # Neutralise the duplicate (recoverable disable by default).
                for label, dup_uuid in (('main', dup_main_uuid), ('_wl', dup_wl_uuid)):
                    if not dup_uuid or dup_uuid == legacy_user.uuid:
                        continue
                    try:
                        if args.delete_duplicates:
                            await api.delete_user(dup_uuid)
                        else:
                            await api.disable_user(dup_uuid)
                        print(f'      duplicate {label} {dup_uuid} {dup_action.lower()}d.')
                    except Exception as e:
                        msg = str(e).lower()
                        if not ('already' in msg or 'not found' in msg or 'not exist' in msg or '404' in msg):
                            print(f'      WARN: could not {dup_action.lower()} duplicate {label} {dup_uuid}: {e}')

                healed += 1
                print(f'      HEALED -> repointed to {legacy_user.uuid}, new link {sub.subscription_url}')

    print(
        f'\n=== summary [{mode}] ===\n'
        f'  victims found:        {victims}\n'
        f'  healed:               {healed}\n'
        f'  ambiguous (manual):   {ambiguous}\n'
        f'  skipped (ownership):  {skipped_owner}'
    )
    if not args.apply and victims:
        print('\nDry-run only. Re-run with --apply to repoint these subscriptions.')
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
