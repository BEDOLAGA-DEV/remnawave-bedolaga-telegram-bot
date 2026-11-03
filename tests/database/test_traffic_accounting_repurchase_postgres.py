"""PostgreSQL cleanup → paid conversion → worker recovery regressions."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from app.database.models import Base, Subscription, User
from tests.fixtures.postgres_db import postgres_sessions
from tests.services import test_traffic_accounting_repurchase as checks


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    'case',
    [
        'existing',
        'missing',
        'rollback',
        'retired_again',
        'epoch',
        'owner',
        'unlimited',
        'admin_unlimited',
        'planned_renewal',
        'bonus_days',
    ],
)
async def test_repurchase_after_cleanup(postgres_database, monkeypatch, case):
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
    if case == 'bonus_days':
        await checks.test_bonus_days_do_not_rearm_retired_zero_limit(monkeypatch)
    elif case == 'admin_unlimited':
        await checks.test_admin_classic_purchase_after_cleanup_rearms_zero_limit(monkeypatch)
    elif case == 'planned_renewal':
        await checks.test_paid_same_limit_renewal_rearms_planned_suspension(monkeypatch)
    elif case == 'unlimited':
        await checks.test_cleanup_then_same_unlimited_tariff_conversion_rearms_recovery(monkeypatch)
    elif case in ('existing', 'missing'):
        await checks.test_cleanup_then_paid_purchase_is_delivered_by_worker(monkeypatch, case == 'missing')
    elif case in ('rollback', 'retired_again'):
        await checks.test_uncommitted_or_retired_purchase_cannot_resume_recovery(monkeypatch, case == 'retired_again')
    else:
        await checks.test_paid_link_recovery_refuses_foreign_or_reused_account(monkeypatch, case)
