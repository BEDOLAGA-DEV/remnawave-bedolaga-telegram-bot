"""Run the money/grant rollback and replay contract against PostgreSQL."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from app.database.models import Base, Subscription, User
from tests.fixtures.postgres_db import postgres_sessions
from tests.services import test_traffic_accounting_store as store_fixtures
from tests.services.test_managed_traffic_reset_hooks import (
    test_paid_managed_reset_is_atomic_and_same_confirmation_charges_once as _check_paid_reset,
)


pytestmark = pytest.mark.postgres


async def test_managed_paid_reset_atomicity_and_confirmation_replay(postgres_database, monkeypatch):
    @asynccontextmanager
    async def database(_monkeypatch):
        async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=1) as sessions:
            db = sessions[0]
            now = datetime.now(UTC)
            db.add(User(id=1))
            db.add(Subscription(id=1, user_id=1, start_date=now, end_date=now + timedelta(days=30)))
            await db.commit()
            yield db

    monkeypatch.setattr(store_fixtures, 'database', database)
    await _check_paid_reset(monkeypatch)
