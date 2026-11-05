"""Managed user switches retain their money/allowance invariants on PostgreSQL."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from app.database.models import Base, Subscription, User
from tests.fixtures.postgres_db import postgres_sessions
from tests.services import test_managed_user_tariff_switch as checks


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('endpoint', ['cabinet', 'miniapp'])
@pytest.mark.parametrize('case', ['paid', 'no_reset', 'free', 'unlimited', 'rollback', 'legacy_package'])
async def test_managed_user_switch_postgres(postgres_database, monkeypatch, endpoint, case):
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
    if case == 'rollback':
        await checks.test_switch_grant_failure_rolls_back_money_and_keeps_packages(monkeypatch, endpoint)
    elif case == 'legacy_package':
        await checks.test_legacy_source_with_package_migrates_before_switch_charge(monkeypatch, endpoint)
    else:
        await checks.test_managed_switch_retires_packages_using_existing_reset_policy(
            monkeypatch,
            endpoint,
            case != 'no_reset',
            0 if case == 'free' else 100,
            0 if case == 'unlimited' else 20,
            multi=True,
        )
