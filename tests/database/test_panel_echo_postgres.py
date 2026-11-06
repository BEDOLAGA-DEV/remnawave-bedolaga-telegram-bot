"""Verify panel echo convergence through durable PostgreSQL accounting state."""

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from app.database.models import Base, Subscription, User
from tests.fixtures.postgres_db import postgres_sessions
from tests.services.panel_sync import test_managed_write_masks as checks


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    'check',
    [
        checks.test_unchanged_panel_echo_never_causes_another_patch,
        checks.test_echo_still_repairs_real_quota_drift,
        checks.test_modified_webhook_echo_converges,
    ],
)
async def test_panel_echo_converges(postgres_database, monkeypatch, check):
    @asynccontextmanager
    async def database(_monkeypatch):
        async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=1) as sessions:
            db = sessions[0]
            db.add(User(id=1))
            db.add(Subscription(id=1, user_id=1, start_date=checks.NOW, end_date=checks.NOW + timedelta(days=30)))
            await db.commit()
            yield db

    monkeypatch.setattr(checks, 'database', database)
    await check(monkeypatch)
