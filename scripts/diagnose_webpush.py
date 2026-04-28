#!/usr/bin/env python3
"""Diagnose Web Push configuration.

Run inside the bot container (so DB and settings are reachable):

    docker compose exec bot python scripts/diagnose_webpush.py

Prints the live values of WEB_PUSH_ENABLED, WEB_PUSH_VAPID_PUBLIC_KEY,
WEB_PUSH_VAPID_EMAIL, WEB_PUSH_VAPID_PRIVATE_KEY (path/PEM preview) plus
what the WebPushService sees at runtime. If anything is wrong (disabled
flag, missing key, unreadable PEM file) it's flagged explicitly.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Make repo root importable when running as `python scripts/diagnose_webpush.py`
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


async def main() -> int:
    # Late imports so the script boots even if env is slightly off.
    from app.config import settings, ENV_OVERRIDE_KEYS
    from app.database.database import get_async_session

    print('=' * 72)
    print('Web Push configuration diagnostics')
    print('=' * 72)

    # ---- 1. Live settings values (post-DB-merge) ----
    print()
    print('[1/4] settings object (after DB apply):')
    print(f'  WEB_PUSH_ENABLED           = {settings.WEB_PUSH_ENABLED!r}')
    print(f'  WEB_PUSH_VAPID_PUBLIC_KEY  = {_preview(settings.WEB_PUSH_VAPID_PUBLIC_KEY)}')
    print(f'  WEB_PUSH_VAPID_EMAIL       = {settings.WEB_PUSH_VAPID_EMAIL!r}')
    print(f'  WEB_PUSH_VAPID_PRIVATE_KEY = {settings.WEB_PUSH_VAPID_PRIVATE_KEY!r}')

    # ---- 2. Which keys are env-override (.env wins over DB) ----
    print()
    print('[2/4] ENV_OVERRIDE_KEYS (keys in .env that block DB overrides):')
    web_push_keys = [
        'WEB_PUSH_ENABLED',
        'WEB_PUSH_VAPID_PUBLIC_KEY',
        'WEB_PUSH_VAPID_EMAIL',
        'WEB_PUSH_VAPID_PRIVATE_KEY',
    ]
    for key in web_push_keys:
        in_env = key in ENV_OVERRIDE_KEYS
        print(f'  {key:30s} -> {"ENV (locked)" if in_env else "DB-editable"}')

    # ---- 3. Raw DB rows ----
    print()
    print('[3/4] system_settings rows for WEB_PUSH_*:')
    try:
        from sqlalchemy import select
        from app.database.models import SystemSetting

        async for db in get_async_session():
            result = await db.execute(
                select(SystemSetting).where(SystemSetting.key.like('WEB_PUSH_%'))
            )
            rows = list(result.scalars())
            if not rows:
                print('  (no rows)')
            for row in rows:
                value_preview = _preview(str(row.value)) if row.value is not None else 'None'
                print(f'  {row.key:30s} = {value_preview}   (type={row.value_type})')
            break  # one session is enough
    except Exception as exc:
        print(f'  [WARN] failed to query DB: {exc}')

    # ---- 4. WebPushService runtime view ----
    print()
    print('[4/4] WebPushService runtime view:')
    try:
        from app.services.web_push_service import web_push_service

        print(f'  is_enabled                = {web_push_service.is_enabled}')
        print(f'  public_key                = {_preview(web_push_service.public_key)}')
        # Access private accessors directly for diagnostics
        print(f'  _enabled (from settings)  = {web_push_service._enabled}')
        print(f'  _public_key               = {_preview(web_push_service._public_key)}')
        print(f'  _email                    = {web_push_service._email!r}')
        priv_pem = web_push_service._private_key
        if not priv_pem:
            print('  _private_key              = [EMPTY] — check file path or PEM value')
        elif priv_pem.lstrip().startswith('-----BEGIN'):
            print('  _private_key              = [PEM loaded, %d bytes]' % len(priv_pem))
        else:
            print('  _private_key              = [not a PEM, passed through verbatim]')
        print(f'  _pywebpush_available      = {web_push_service._pywebpush_available}')
    except Exception as exc:
        print(f'  [WARN] failed to inspect service: {exc}')

    print()
    print('=' * 72)
    if not settings.WEB_PUSH_ENABLED:
        print('[!] WEB_PUSH_ENABLED is False — flip it to true in the admin panel')
        print('    (or set via DB: UPDATE system_settings SET value=\'true\' WHERE key=\'WEB_PUSH_ENABLED\')')
    elif not settings.WEB_PUSH_VAPID_PUBLIC_KEY:
        print('[!] WEB_PUSH_VAPID_PUBLIC_KEY is empty — re-paste it in admin panel')
    elif not settings.WEB_PUSH_VAPID_PRIVATE_KEY:
        print('[!] WEB_PUSH_VAPID_PRIVATE_KEY missing from .env — add:')
        print('    WEB_PUSH_VAPID_PRIVATE_KEY=data/vapid_private_key.pem')
    else:
        print('[OK] config looks good. Restart the bot if the service still')
        print('     reports disabled (pre-fix versions cached values at import time).')
    print('=' * 72)
    return 0


def _preview(value: str, limit: int = 40) -> str:
    if not value:
        return "''"
    text = str(value)
    if len(text) <= limit:
        return repr(text)
    return repr(text[:limit] + f'... [{len(text)} total]')


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
