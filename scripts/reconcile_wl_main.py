#!/usr/bin/env python3
"""Find and disable/delete stale ACTIVE _wl (БС-трафик) RemnaWave accounts.

Background
----------
Each active subscription has a MAIN RemnaWave account (subscription.remnawave_uuid)
and exactly one paired _wl account named '<main_username>_wl'. Older naming
conventions (template changes 'user_{telegram_id}' -> 'u_{telegram_id}',
per-subscription '_<sub_id>' / '_<short_id>' suffixes) left EXTRA _wl accounts
alive on the panel. When such a leftover stays ACTIVE it keeps serving
БС-трафик even though no current subscription points at it.

This tool finds those orphans and disables (or deletes) them. It is deliberately
conservative:

- It NEVER touches the keeper _wl of any active subscription. The keeper set is
  built FIRST, across ALL of a user's active subscriptions (resolved from each
  bound main's current panel username), so one subscription's cleanup can never
  remove another subscription's real _wl.
- It only acts on ACTIVE orphans. Already-disabled/expired leftovers are
  harmless and left untouched.
- It does NOT repoint or modify main accounts. The bound main is always treated
  as correct (a per-subscription 'u_<tg>_<short>' main is legitimate in
  multi-tariff mode, not a duplicate).

Usage (inside the bot container)
--------------------------------
    docker compose exec bot python scripts/reconcile_wl_main.py
        -> DRY-RUN: report active orphan _wl accounts, change nothing.

    docker compose exec bot python scripts/reconcile_wl_main.py --apply
        -> disable the orphans (recoverable).

    docker compose exec bot python scripts/reconcile_wl_main.py --apply --delete
        -> delete the orphans instead of disabling (use after a soak period).

    docker compose exec bot python scripts/reconcile_wl_main.py --telegram-id 5178677268
        -> limit to specific telegram_id(s); repeatable.

    docker compose exec bot python scripts/reconcile_wl_main.py --diagnose --telegram-id 5178677268
        -> read-only dump of a user's main / _wl / legacy accounts.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import defaultdict
from pathlib import Path

import structlog

# Make repo root importable when running as `python scripts/reconcile_wl_main.py`.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from app.config import settings  # noqa: E402
from app.database.database import AsyncSessionLocal  # noqa: E402
from app.database.models import Subscription, SubscriptionStatus  # noqa: E402
from app.external.remnawave_api import UserStatus  # noqa: E402
from app.services.subscription_service import SubscriptionService  # noqa: E402
from app.services.system_settings_service import bot_configuration_service  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description='Disable/delete stale ACTIVE _wl RemnaWave accounts.')
    p.add_argument('--apply', action='store_true', help='Apply changes (default: dry-run, report only).')
    p.add_argument(
        '--delete', '--delete-duplicates', dest='delete', action='store_true',
        help='Delete orphans instead of disabling them (use only after a soak period).',
    )
    p.add_argument(
        '--diagnose', action='store_true',
        help='Read-only: dump main / _wl / legacy account state for the selected subscriptions and exit.',
    )
    p.add_argument(
        '--telegram-id', type=int, action='append', default=None,
        help='Limit to specific telegram_id(s). Repeatable. Default: all users.',
    )
    return p.parse_args()


def _legacy_bases(user) -> list[str]:
    """Candidate legacy MAIN usernames (current template form + historical 'user_<tg>')."""
    bases: list[str] = []
    try:
        tpl = settings.format_remnawave_username(
            full_name=user.full_name,
            username=user.username,
            telegram_id=user.telegram_id,
            email=user.email,
            user_id=user.id,
        )
        if tpl:
            bases.append(tpl)
    except Exception:
        pass
    if user.telegram_id:
        legacy = f'user_{user.telegram_id}'
        if legacy not in bases:
            bases.append(legacy)
    return bases


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


def _candidate_wl_names(tg: int, sub_ids: set, short_ids: set) -> list[str]:
    """All historical _wl name forms for a user's subscriptions, across both prefixes."""
    bases = {f'u_{tg}', f'user_{tg}'}
    for sid in sub_ids:
        bases |= {f'u_{tg}_{sid}', f'user_{tg}_{sid}'}
    for shid in short_ids:
        if shid:
            bases |= {f'u_{tg}_{shid}', f'user_{tg}_{shid}'}
    return sorted({f'{b}_wl' for b in bases})


async def _diagnose(api, svc, subs) -> None:
    for sub in subs:
        user = sub.user
        print(f'\nsub {sub.id}  tg={getattr(user, "telegram_id", None)}  status={sub.status}')
        print(f'   subscription.remnawave_uuid = {sub.remnawave_uuid}')
        main_user = await api.get_user_by_uuid(sub.remnawave_uuid) if sub.remnawave_uuid else None
        main_name = getattr(main_user, 'username', None)
        print(f'   main on panel:         username={main_name!r} uuid={getattr(main_user, "uuid", None)} status={getattr(getattr(main_user, "status", None), "value", None)}')
        if main_name:
            wl_name = svc._derive_wl_username(main_name, None, None)
            wl = await api.get_user_by_username(wl_name)
            print(f'   paired _wl (<main>_wl): {wl_name!r} exists={bool(wl)} uuid={getattr(wl, "uuid", None)} status={getattr(getattr(wl, "status", None), "value", None)}')
        if user and user.telegram_id:
            for base in _legacy_bases(user):
                acc = await api.get_user_by_username(base)
                print(f'   legacy acct:           {base!r} exists={bool(acc)} uuid={getattr(acc, "uuid", None)} status={getattr(getattr(acc, "status", None), "value", None)}')
                base_wl = svc._derive_wl_username(base, None, None)
                acc_wl = await api.get_user_by_username(base_wl)
                print(f'   legacy _wl:            {base_wl!r} exists={bool(acc_wl)} uuid={getattr(acc_wl, "uuid", None)} status={getattr(getattr(acc_wl, "status", None), "value", None)}')


async def main() -> int:
    args = _parse_args()
    mode = 'APPLY' if args.apply else 'DRY-RUN'
    action = 'DELETE' if args.delete else 'DISABLE'

    # Quiet logs. The app uses structlog (stdlib logging.disable alone does not
    # affect it), and lookups would otherwise log expected 404 misses. Raise the
    # structlog level to ERROR for this CLI run; the script reports via print().
    logging.disable(logging.WARNING)
    try:
        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))
    except Exception:
        pass

    # Apply admin-panel (DB) setting overrides onto the `settings` object — this
    # is a separate process from the bot, so MULTI_TARIFF_ENABLED,
    # REMNAWAVE_USER_USERNAME_TEMPLATE, etc. would otherwise be the static env
    # defaults rather than the values configured from the bot admin panel.
    await bot_configuration_service.initialize()

    if not settings.is_multi_tariff_enabled():
        print(
            'Multi-tariff mode is disabled — this bug class does not apply. Nothing to do.\n'
            f'(effective: MULTI_TARIFF_ENABLED={settings.MULTI_TARIFF_ENABLED}, SALES_MODE={settings.SALES_MODE!r})'
        )
        return 0

    print(f'(template: {settings.REMNAWAVE_USER_USERNAME_TEMPLATE!r})')
    print(f'=== reconcile_wl_main [{mode}] — active orphans will be {action}d ===\n')

    svc = SubscriptionService()
    found = 0
    acted = 0

    async with AsyncSessionLocal() as db:
        subs = await _load_candidates(db, args.telegram_id)
        print(f'Scanning {len(subs)} active/trial subscription(s) with a panel main account...')

        async with svc.get_api_client() as api:
            # Skip the per-lookup happ crypto-link fetch (a slow network call to
            # crypto.happ.su that also floods the log) — irrelevant here.
            async def _skip_enrich(u, *a, **k):
                return u

            api.enrich_user_with_happ_link = _skip_enrich  # type: ignore[assignment]

            if args.diagnose:
                await _diagnose(api, svc, subs)
                return 0

            # Group active subs by user so the keeper-_wl set spans ALL of a
            # user's subscriptions (prevents removing another sub's real _wl).
            by_user: dict[int, list] = defaultdict(list)
            for sub in subs:
                if sub.user and sub.user.telegram_id:
                    by_user[sub.user.telegram_id].append(sub)

            for tg, usubs in sorted(by_user.items()):
                # 1. Build the keeper _wl uuid set from every active sub's bound main.
                keeper_wl: set[str] = set()
                sub_ids: set = set()
                short_ids: set = set()
                unresolved = False  # could not fully resolve some active sub's main/_wl
                for sub in usubs:
                    sub_ids.add(sub.id)
                    shid = getattr(sub, 'remnawave_short_id', None)
                    if shid:
                        short_ids.add(shid)
                    if not sub.remnawave_uuid:
                        unresolved = True
                        continue
                    try:
                        main_user = await api.get_user_by_uuid(sub.remnawave_uuid)
                        main_name = getattr(main_user, 'username', None)
                        if not main_name:
                            unresolved = True
                            continue
                        wl = await api.get_user_by_username(svc._derive_wl_username(main_name, None, None))
                    except Exception as e:
                        unresolved = True
                        print(f'  tg {tg}: main/_wl lookup failed for sub {sub.id} ({e}).')
                        continue
                    if wl and getattr(wl, 'uuid', None):
                        keeper_wl.add(wl.uuid)

                # Fail-safe: if any active sub's keeper _wl could not be resolved
                # (main 404'd or a lookup errored), do NOT flag orphans for this
                # user — otherwise a live subscription's _wl could be removed.
                if unresolved:
                    print(f'  tg {tg}: SKIPPED — could not resolve all active mains/_wl (fail-safe, no changes).')
                    continue

                # 2. Enumerate every historical _wl name; flag non-keeper orphans.
                #    Disable mode targets only ACTIVE leftovers (no point disabling
                #    an already-disabled one). Delete mode targets ANY status, so a
                #    prior `--apply` (disable) sweep can be finalised later with
                #    `--apply --delete` — otherwise the disabled orphans would be
                #    invisible to the delete pass.
                seen: set[str] = set()
                orphans: list = []
                for name in _candidate_wl_names(tg, sub_ids, short_ids):
                    try:
                        acc = await api.get_user_by_username(name)
                    except Exception:
                        continue
                    au = getattr(acc, 'uuid', None)
                    if not au or au in keeper_wl or au in seen:
                        continue
                    seen.add(au)
                    st = getattr(acc, 'status', None)
                    if not args.delete and st != UserStatus.ACTIVE:
                        continue  # disable mode: skip already-disabled leftovers
                    orphans.append((name, acc, st))

                if not orphans:
                    continue

                scope = 'orphan' if args.delete else 'active orphan'
                print(f'\ntg {tg}: {len(orphans)} {scope} _wl account(s)')
                for name, acc, st in orphans:
                    found += 1
                    st_label = getattr(st, 'value', st)
                    print(f'   {name!r} uuid={acc.uuid} status={st_label}' + ('' if args.apply else ' (dry-run)'))
                    if not args.apply:
                        continue
                    try:
                        if args.delete:
                            await api.delete_user(acc.uuid)
                        else:
                            await api.disable_user(acc.uuid)
                        acted += 1
                        print(f'      -> {action.lower()}d.')
                    except Exception as e:
                        msg = str(e).lower()
                        if not ('already' in msg or 'not found' in msg or 'not exist' in msg or '404' in msg):
                            print(f'      WARN: could not {action.lower()} {name}: {e}')

    scope_label = 'orphan _wl found' if args.delete else 'active orphan _wl found'
    print(
        f'\n=== summary [{mode}] ===\n'
        f'  {scope_label}: {found}\n'
        f'  {action.lower()}d: {acted}'
    )
    if not args.apply and found:
        print(f'\nDry-run only. Re-run with --apply to {action.lower()} these orphans.')
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
