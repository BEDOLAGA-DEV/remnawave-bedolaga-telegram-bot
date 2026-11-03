"""Classic traffic selection keeps the debit and ledger in one PostgreSQL commit."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from app.database.models import Base, Subscription, User
from tests.fixtures.postgres_db import postgres_sessions
from tests.services import test_managed_classic_traffic_switch as checks


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('endpoint', ['cabinet', 'miniapp'])
@pytest.mark.parametrize('target', [100, 50, 0, 'rollback'])
async def test_managed_classic_switch_postgres(postgres_database, monkeypatch, endpoint, target):
    @asynccontextmanager
    async def database(_monkeypatch):
        async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=1) as sessions:
            db = sessions[0]
            now = datetime.now(UTC)
            db.add(User(id=1))
            db.add(Subscription(id=1, user_id=1, start_date=now, end_date=now + timedelta(days=30)))
            await db.commit()
            yield db

    monkeypatch.setattr(checks, 'database', database)
    if target == 'rollback':
        await checks.test_classic_switch_ledger_failure_rolls_back_debit_and_rights(monkeypatch, endpoint)
    else:
        await checks.test_classic_switch_preserves_spent_base_and_existing_package_policy(monkeypatch, endpoint, target)
