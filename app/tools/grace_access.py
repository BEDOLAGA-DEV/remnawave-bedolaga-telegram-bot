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

from sqlalchemy import func, select

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import GraceAccessSessionModel
from app.services.grace_access_runtime import grace_access_runtime
from app.services.grace_access_service import GraceAccessMode


async def _status() -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        state_rows = (
            await db.execute(
                select(GraceAccessSessionModel.state, func.count())
                .group_by(GraceAccessSessionModel.state)
                .order_by(GraceAccessSessionModel.state)
            )
        ).all()
        open_error_count = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(GraceAccessSessionModel)
                    .where(
                        GraceAccessSessionModel.state.in_(('pending', 'active', 'restoring')),
                        GraceAccessSessionModel.last_error.isnot(None),
                    )
                )
            ).scalar_one()
        )
        completed_error_count = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(GraceAccessSessionModel)
                    .where(
                        GraceAccessSessionModel.state == 'completed',
                        GraceAccessSessionModel.last_error.isnot(None),
                    )
                )
            ).scalar_one()
        )
        error_rows = (
            await db.execute(
                select(
                    GraceAccessSessionModel.id,
                    GraceAccessSessionModel.subscription_id,
                    GraceAccessSessionModel.state,
                    GraceAccessSessionModel.completion_reason,
                    GraceAccessSessionModel.last_error,
                )
                .where(GraceAccessSessionModel.last_error.isnot(None))
                .order_by(GraceAccessSessionModel.updated_at.desc())
                .limit(20)
            )
        ).all()
    states = {str(state): int(count) for state, count in state_rows}
    open_count = sum(states.get(state, 0) for state in ('pending', 'active', 'restoring'))
    recent_errors = [
        {
            'id': str(session_id),
            'subscription_id': int(subscription_id),
            'state': str(state),
            'completion_reason': str(completion_reason) if completion_reason else None,
            'last_error': str(last_error),
        }
        for session_id, subscription_id, state, completion_reason, last_error in error_rows
    ]
    return {
        'open': open_count,
        'open_errors': open_error_count,
        'completed_errors': completed_error_count,
        'with_errors': open_error_count + completed_error_count,
        'states': states,
        'recent_errors': recent_errors,
    }


async def _restore_all(*, apply: bool, accept_conflicts: bool) -> int:
    before = await _status()
    print(json.dumps({'operation': 'restore-all', 'apply': apply, 'before': before}, ensure_ascii=False, indent=2))
    if not apply:
        print('Dry-run only. Add --apply to restore open grace sessions immediately.')
        return 0

    if GraceAccessMode.parse(settings.GRACE_ACCESS_MODE) is GraceAccessMode.ACTIVE:
        print('Refusing restore-all while GRACE_ACCESS_MODE=true. Switch the bot to drain and restart it first.')
        return 2

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
