"""Command scope cleanup survives process restarts on PostgreSQL."""

from contextlib import asynccontextmanager

import pytest

from tests.fixtures import sqlite_memory
from tests.fixtures.postgres_db import postgres_sessions
from tests.services.test_bot_commands import test_store_survives_restart_and_separates_bots as check_store


pytestmark = pytest.mark.postgres


async def test_command_scope_journal(postgres_database, monkeypatch):
    @asynccontextmanager
    async def database(_monkeypatch, tables):
        async with postgres_sessions(postgres_database, tables, count=1) as sessions:
            yield sessions[0]

    monkeypatch.setattr(sqlite_memory, 'memory_session', database)
    await check_store(monkeypatch)
