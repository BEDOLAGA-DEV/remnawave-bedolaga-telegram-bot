"""Regression test for Telegram bug #649289.

Restore's TRUNCATE ... CASCADE needs an ACCESS EXCLUSIVE lock, which conflicts with locks
the live bot/cabinet hold on the same tables — so it waited out lock_timeout and failed
with LockNotAvailableError ("Ошибка TRUNCATE CASCADE, пробуем поштучно"), and the per-table
fallback hit the same wall. _terminate_competing_backends() now frees the locks by dropping
other sessions before TRUNCATE; it is best-effort so a role without signal privilege keeps
the prior behaviour rather than crashing the restore.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import backup_service as bs


@pytest.mark.asyncio
async def test_terminates_other_sessions_and_counts():
    result = MagicMock()
    result.fetchall = MagicMock(return_value=[(1,), (2,), (3,)])
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=result)

    terminated = await bs._terminate_competing_backends(conn)

    assert terminated == 3
    sql = str(conn.execute.call_args[0][0])
    assert 'pg_terminate_backend' in sql
    assert 'current_database()' in sql
    assert 'pg_backend_pid()' in sql  # never terminate our own restore connection


@pytest.mark.asyncio
async def test_best_effort_on_privilege_error():
    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=RuntimeError('must be superuser to terminate'))

    # Must not raise — restore proceeds with the old TRUNCATE-and-wait behaviour.
    assert await bs._terminate_competing_backends(conn) == 0
