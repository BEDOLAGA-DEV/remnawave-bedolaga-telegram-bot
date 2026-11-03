"""Real PostgreSQL coverage of the three daily resume money boundaries."""

from contextlib import asynccontextmanager

import pytest

from tests.fixtures.postgres_db import postgres_session
from tests.services import test_managed_daily_resume as checks


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('endpoint', ['cabinet', 'miniapp', 'telegram'])
@pytest.mark.parametrize('failure', [False, True])
@pytest.mark.parametrize('reset', [False, True])
async def test_managed_daily_resume_postgres(postgres_database, monkeypatch, endpoint, failure, reset):
    @asynccontextmanager
    async def session(_monkeypatch, tables):
        async with postgres_session(postgres_database, tables) as db:
            yield db

    monkeypatch.setattr(checks, 'memory_session', session)
    await checks.test_managed_daily_resume_atomic(monkeypatch, endpoint, failure, reset)
