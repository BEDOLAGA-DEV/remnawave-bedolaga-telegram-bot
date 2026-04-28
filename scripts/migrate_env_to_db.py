#!/usr/bin/env python3
"""Migrate runtime settings from .env to the system_settings DB table.

Why: this project lets admins edit almost every setting from the admin panel,
but any key explicitly present in .env overrides DB (see ENV_OVERRIDE_KEYS in
app/config.py). So keys duplicated in .env silently block admin-panel edits.
This utility moves runtime values out of .env into system_settings, leaving
only infrastructure keys in the file. After the migration, admin-panel changes
actually take effect.

Usage (from repo root, with the bot's venv / docker python):

    python scripts/migrate_env_to_db.py --dry-run          # preview
    python scripts/migrate_env_to_db.py                    # apply
    python scripts/migrate_env_to_db.py --env-file .env --output .env.new
    python scripts/migrate_env_to_db.py --write-env        # overwrite .env
                                                           # (backup saved)

Flow:
    1. Parse the .env file (python-dotenv).
    2. For each key:
       - If in INFRASTRUCTURE_KEYS / infrastructure prefixes -> keep in .env.
       - Else if key is a known Settings field -> upsert into system_settings,
         drop from .env.
       - Else (unknown key) -> keep in .env, log a warning.
    3. Write a new .env preserving comments / ordering / untouched keys.
    4. If --write-env: back up original to .env.pre-migration.bak, then
       overwrite. Otherwise leave a sibling file (default: .env.new).
    5. Report a summary.

Idempotent: safe to run again on prod with a different DB. Re-running after
the first pass is a no-op because migrated keys are no longer in .env.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

# Make repo root importable when running as `python scripts/migrate_env_to_db.py`.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import dotenv_values  # noqa: E402

# Infrastructure keys that MUST stay in .env. These are either secrets, bind
# addresses, or bootstrap values the app needs before the DB is reachable.
INFRASTRUCTURE_KEYS: set[str] = {
    # Bot auth & ownership
    'BOT_TOKEN',
    'ADMIN_IDS',
    # Database connection
    'DATABASE_URL',
    'POSTGRES_HOST',
    'POSTGRES_PORT',
    'POSTGRES_DB',
    'POSTGRES_USER',
    'POSTGRES_PASSWORD',
    # Redis
    'REDIS_URL',
    # Bot web server bind
    'WEB_API_HOST',
    'WEB_API_PORT',
    # Webhook transport (bot-mode secrets / bind-time config)
    'WEBHOOK_URL',
    'WEBHOOK_PATH',
    'WEBHOOK_SECRET_TOKEN',
    # Cabinet bootstrap (JWT secret, CORS, port, public URL)
    'CABINET_ENABLED',
    'CABINET_JWT_SECRET',
    'CABINET_ALLOWED_ORIGINS',
    'CABINET_PORT',
    'CABINET_URL',
    'CABINET_HOST',
    'CABINET_COOKIE_DOMAIN',
    'CABINET_COOKIE_SECURE',
    # RemnaWave connection (secrets)
    'REMNAWAVE_API_URL',
    'REMNAWAVE_API_KEY',
    'REMNAWAVE_SECRET_KEY',
    'REMNAWAVE_USERNAME',
    'REMNAWAVE_PASSWORD',
    'REMNAWAVE_AUTH_TYPE',
    # External admin bridge (read-only in the app)
    'EXTERNAL_ADMIN_TOKEN',
    'EXTERNAL_ADMIN_TOKEN_BOT_ID',
    # SMTP secrets — these should stay in env; the admin panel has dedicated UI
    # to configure SMTP but a migration would expose credentials in DB rows.
    # Users can choose to migrate manually if they prefer the DB-driven flow.
    'SMTP_HOST',
    'SMTP_PORT',
    'SMTP_USER',
    'SMTP_PASSWORD',
    'SMTP_FROM_EMAIL',
    'SMTP_USE_TLS',
    # Web Push — private key path is sensitive
    'WEB_PUSH_VAPID_PRIVATE_KEY',
    # Runtime paths and env meta
    'LOCALES_PATH',
    'TZ',
    'LOG_LEVEL',
    'SENTRY_DSN',
    # Docker / compose meta often present here
    'COMPOSE_PROJECT_NAME',
}

# Keys that are in Settings but we still refuse to migrate (mirrors
# BotConfigurationService.EXCLUDED_KEYS + read-only).
NEVER_MIGRATE: set[str] = {
    'BOT_TOKEN',
    'ADMIN_IDS',
    'EXTERNAL_ADMIN_TOKEN',
    'EXTERNAL_ADMIN_TOKEN_BOT_ID',
}


def _load_settings_fields() -> set[str]:
    """Import Settings and return the set of known field names."""
    from app.config import Settings  # noqa: WPS433 (runtime import — needs sys.path)

    return set(Settings.model_fields.keys())


def _classify(
    key: str,
    settings_fields: set[str],
) -> str:
    """Return one of: 'keep', 'migrate', 'unknown'."""
    if key in INFRASTRUCTURE_KEYS:
        return 'keep'
    if key in NEVER_MIGRATE:
        return 'keep'
    if key in settings_fields:
        return 'migrate'
    return 'unknown'


def _rewrite_env_text(original_text: str, keys_to_drop: set[str]) -> str:
    """Return a new .env body with `keys_to_drop` commented out, preserving
    comments, blank lines, and everything else untouched."""
    output_lines: list[str] = []
    for raw_line in original_text.splitlines():
        stripped = raw_line.lstrip()
        # Skip commented / blank lines unchanged.
        if not stripped or stripped.startswith('#'):
            output_lines.append(raw_line)
            continue

        # Parse `KEY=...` — dotenv allows leading `export `.
        after_export = stripped
        if after_export.startswith('export '):
            after_export = after_export[len('export ') :].lstrip()

        eq_pos = after_export.find('=')
        if eq_pos <= 0:
            output_lines.append(raw_line)
            continue

        key = after_export[:eq_pos].strip()
        if key in keys_to_drop:
            # Comment out rather than delete so ops can audit the diff.
            output_lines.append(f'# migrated-to-db: {raw_line}')
        else:
            output_lines.append(raw_line)

    # Preserve original trailing newline convention.
    trailing = '\n' if original_text.endswith('\n') else ''
    return '\n'.join(output_lines) + trailing


async def _upsert_all(
    pairs: list[tuple[str, str | None]],
) -> tuple[int, list[tuple[str, str]]]:
    """Write each (key, raw_value) into system_settings. Returns (ok_count, errors)."""
    from app.database.crud.system_setting import upsert_system_setting  # noqa: WPS433
    from app.database.database import AsyncSessionLocal  # noqa: WPS433

    errors: list[tuple[str, str]] = []
    ok = 0
    async with AsyncSessionLocal() as session:
        for key, value in pairs:
            try:
                await upsert_system_setting(session, key, value)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                errors.append((key, repr(exc)))
        await session.commit()
    return ok, errors


def _format_report(
    to_migrate: list[tuple[str, str | None]],
    kept: list[str],
    unknown: list[str],
) -> str:
    lines: list[str] = []
    lines.append(f'To migrate -> DB  : {len(to_migrate)} keys')
    lines.append(f'Kept in .env      : {len(kept)} keys')
    lines.append(f'Unknown (kept)    : {len(unknown)} keys')
    if to_migrate:
        lines.append('')
        lines.append('--- Will be written to system_settings ---')
        for key, raw in to_migrate:
            preview = '<empty>' if raw in (None, '') else _preview(raw)
            lines.append(f'  {key} = {preview}')
    if unknown:
        lines.append('')
        lines.append('--- Unknown keys (kept in .env, review manually) ---')
        for key in unknown:
            lines.append(f'  {key}')
    return '\n'.join(lines)


def _preview(value: str, max_len: int = 60) -> str:
    if len(value) <= max_len:
        return value
    return value[: max_len - 1] + '…'


async def _run(args: argparse.Namespace) -> int:
    env_path = Path(args.env_file).resolve()
    if not env_path.exists():
        print(f'ERROR: env file not found: {env_path}', file=sys.stderr)
        return 2

    # Make sure Settings can import without a live bot environment.
    os.environ.setdefault('BOT_TOKEN', 'dummy-for-import')
    settings_fields = _load_settings_fields()

    parsed = dotenv_values(env_path)

    to_migrate: list[tuple[str, str | None]] = []
    kept: list[str] = []
    unknown: list[str] = []

    for key, value in parsed.items():
        if key is None:
            continue
        decision = _classify(key, settings_fields)
        if decision == 'keep':
            kept.append(key)
        elif decision == 'migrate':
            to_migrate.append((key, value))
        else:
            unknown.append(key)

    print(_format_report(to_migrate, kept, unknown))
    print()

    if args.dry_run:
        print('Dry run — no changes written.')
        return 0

    if not to_migrate:
        print('Nothing to migrate — .env is already minimal.')
        return 0

    # 1. Write to DB first. If this fails, .env stays unchanged.
    ok, errors = await _upsert_all(to_migrate)
    print(f'DB: upserted {ok} rows')
    if errors:
        print(f'DB: {len(errors)} failures')
        for key, msg in errors:
            print(f'  {key}: {msg}')
        print('Aborting before touching .env — fix DB errors and re-run.')
        return 3

    # 2. Rewrite .env
    migrated_keys = {key for key, _ in to_migrate}
    original = env_path.read_text(encoding='utf-8')
    new_text = _rewrite_env_text(original, migrated_keys)

    if args.write_env:
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        backup = env_path.with_suffix(f'.pre-migration-{timestamp}.bak')
        shutil.copy2(env_path, backup)
        env_path.write_text(new_text, encoding='utf-8')
        print(f'.env overwritten. Backup: {backup.name}')
    else:
        out_path = Path(args.output).resolve()
        out_path.write_text(new_text, encoding='utf-8')
        print(f'New env written to: {out_path}')
        print('Review it and replace .env when satisfied (or re-run with --write-env).')

    print()
    print('Next steps:')
    print('  1. Restart the bot (docker compose restart bot) so ENV_OVERRIDE_KEYS is rebuilt.')
    print('  2. Verify settings in the admin panel now apply.')
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        '--env-file', default='.env', help='Path to the .env file to migrate (default: ./.env)',
    )
    parser.add_argument(
        '--output', default='.env.new',
        help='Where to write the reduced .env when --write-env is not set (default: .env.new)',
    )
    parser.add_argument(
        '--write-env', action='store_true',
        help='Overwrite the source .env (a timestamped .bak is saved first).',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Show what would happen, write nothing.',
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return asyncio.run(_run(args))


if __name__ == '__main__':
    raise SystemExit(main())
