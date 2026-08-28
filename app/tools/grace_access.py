"""Emergency/status CLI for restricted grace access.

Examples:
    python -m app.tools.grace_access status
    python -m app.tools.grace_access restore-all
    python -m app.tools.grace_access restore-all --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.services.grace_access_runtime import collect_grace_status, grace_access_runtime
from app.services.grace_access_service import GraceAccessMode
from app.services.system_settings_service import bot_configuration_service


async def _status() -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        return await collect_grace_status(db)


async def _restore_all(*, apply: bool, accept_conflicts: bool) -> int:
    before = await _status()
    print(json.dumps({'operation': 'restore-all', 'apply': apply, 'before': before}, ensure_ascii=False, indent=2))
    if not apply:
        print('Dry-run only. Add --apply to restore open grace sessions immediately.')
        return 0

    if GraceAccessMode.parse(settings.GRACE_ACCESS_MODE) is GraceAccessMode.ACTIVE:
        print('Refusing restore-all while GRACE_ACCESS_MODE=true. Switch the bot to drain and restart it first.')
        return 2

    # Читается СОХРАНЁННАЯ конфигурация, а работающий процесс мог стартовать с
    # другим режимом: он кэширует его при запуске, и переключение из кабинета
    # вступает в силу только после перезапуска. Проверить это отсюда нельзя —
    # процесс чужой, — поэтому предупреждаем явно.
    print(
        'Note: this reads the stored configuration. A bot process that started before the mode was '
        'changed is still running its old mode — restart it first, or it will keep granting grace '
        'while this command closes sessions.'
    )

    result = await grace_access_runtime.force_restore_all()
    after = await _status()
    summary = {
        'inspected': result.inspected,
        'paid': result.paid,
        'timed_out': result.timed_out,
        'drained': result.drained,
        'revoked': result.revoked,
        'conflicts': result.conflicts,
        'errors': result.errors,
    }
    print(json.dumps({'result': summary, 'after': after}, ensure_ascii=False, indent=2))
    if after['open'] or after['open_errors'] or result.errors:
        print('Restore is incomplete. Keep the new code/table and inspect last_error before rollback.')
        return 2
    if (after['completed_errors'] or result.conflicts) and not accept_conflicts:
        print(
            'All sessions are closed, but terminal conflicts require review. '
            'Inspect recent_errors, verify the affected users in Remnawave, then repeat with --accept-conflicts.'
        )
        return 2
    if after['completed_errors'] or result.conflicts:
        print('Terminal conflicts were explicitly accepted; no open grace session remains.')
    print(
        'All grace sessions are closed. If rolling code back to a revision without migration 0097, '
        'run "alembic downgrade 0096" before deploying the old code.'
    )
    return 0


async def _run(args: argparse.Namespace) -> int:
    # Одноразовому контейнеру тоже нужны настройки из system_settings: адрес и
    # ключ панели редактируются из кабинета, и без загрузки восстановление
    # ушло бы в ненастроенный (или чужой) клиент.
    await bot_configuration_service.initialize(sync_web_api_token=False)

    if args.command == 'status':
        print(json.dumps(await _status(), ensure_ascii=False, indent=2))
        return 0
    return await _restore_all(apply=args.apply, accept_conflicts=args.accept_conflicts)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Grace-access operational CLI')
    subparsers = parser.add_subparsers(dest='command', required=True)
    subparsers.add_parser('status', help='Show grace session counts without changing anything')
    restore = subparsers.add_parser('restore-all', help='Restore all open sessions (dry-run by default)')
    restore.add_argument('--apply', action='store_true', help='Actually perform the emergency restore')
    restore.add_argument(
        '--accept-conflicts',
        action='store_true',
        help='After manual Remnawave verification, accept already-terminal conflicts for rollback',
    )
    return parser


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parser().parse_args())))


if __name__ == '__main__':
    main()
