"""Durable managed renewal/resize intents use the same PostgreSQL money transaction."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from app.database.models import Base, Subscription, User
from tests.fixtures.postgres_db import postgres_sessions
from tests.services import test_traffic_base_grant_service as contract


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('reset_base', [True, False])
async def test_committed_renewal_intent_survives_restart(postgres_database, monkeypatch, reset_base):
    @asynccontextmanager
    async def database(_monkeypatch):
        async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=1) as sessions:
            db = sessions[0]
            now = datetime.now(UTC)
            db.add(User(id=1))
            db.add(Subscription(id=1, user_id=1, start_date=now, end_date=now + timedelta(days=30)))
            await db.commit()
            yield db

    monkeypatch.setattr(contract, 'database', database)
    await contract.test_committed_intent_is_applied_once_after_restart(
        monkeypatch,
        reset_base,
        1024**3 if reset_base else 0,
        'current',
    )
