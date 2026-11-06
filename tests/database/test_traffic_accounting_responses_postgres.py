"""Pending paid mode changes retain truthful nullable quota on PostgreSQL."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from app.database.models import Base, Subscription, User
from tests.cabinet.test_traffic_accounting_responses import test_persisted_pending_mode_switch_reports_unknown as check
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('old_limit,new_limit,phase', [(0, 100, 'unlimited'), (100, 0, 'ready')])
async def test_pending_mode_summary(postgres_database, monkeypatch, old_limit, new_limit, phase):
    @asynccontextmanager
    async def database(_monkeypatch):
        async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
            now = datetime.now(UTC)
            db.add(User(id=1))
            db.add(Subscription(id=1, user_id=1, start_date=now, end_date=now + timedelta(days=30)))
            await db.commit()
            yield db

    monkeypatch.setattr('tests.services.test_traffic_accounting_store.database', database)
    await check(monkeypatch, old_limit, new_limit, phase)
