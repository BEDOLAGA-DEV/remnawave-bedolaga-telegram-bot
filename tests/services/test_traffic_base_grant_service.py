"""A paid base grant survives a crash between financial commit and panel sync."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import Subscription
from app.services.traffic_accounting import Allowance, TrafficAccount
from app.services.traffic_accounting_store import initialize_account, load_account
from tests.services.test_traffic_accounting_store import database


@pytest.mark.parametrize(('reset_base', 'expected_base'), [(True, 1024**3), (False, 0)])
@pytest.mark.parametrize('scenario', ['current', 'expired', 'superseded'])
async def test_committed_intent_is_applied_once_after_restart(monkeypatch, reset_base, expected_base, scenario):
    from app.services import traffic_accounting_service as accounting, traffic_base_grant_service as grants

    now = datetime.now(UTC)
    async with database(monkeypatch) as db:
        sub = await db.get(Subscription, 1)
        sub.traffic_accounting_mode = 'managed'
        sub.traffic_limit_gb = 2
        sub.purchased_traffic_gb = 1
        state = await initialize_account(
            db,
            1,
            TrafficAccount(
                'panel',
                100,
                100,
                (
                    Allowance('base:old', 0, now + timedelta(days=2)),
                    Allowance('purchase:1', 20, now + timedelta(days=2)),
                ),
            ),
            strategy='NO_RESET',
            timezone='UTC',
            anchor=now,
            next_refresh_at=None,
        )
        state.phase = 'ready'
        state.base_budget_bytes = 1024**3
        await db.commit()
        intent = await grants.stage_paid_base_grant(db, sub, operation_key='payment:1', reset_base=reset_base)
        if scenario == 'expired':
            intent.expires_at = now - timedelta(seconds=1)
            expected_base = 0
        elif scenario == 'superseded':
            state.last_base_operation_at = now + timedelta(milliseconds=500)
            expected_base = 0
        await db.commit()
        account, _ = await load_account(db, 1)
        assert account.remaining_bytes(now) == 20
        monkeypatch.setattr(accounting, 'reconcile_account_locked', AsyncMock(return_value=account))
        await grants.apply_pending_base_grants_locked(db, sub, SimpleNamespace(), now=now + timedelta(seconds=1))
        await db.commit()
        account, _ = await load_account(db, 1)
        assert account.remaining_bytes(now) == expected_base + 20
        await grants.apply_pending_base_grants_locked(db, sub, SimpleNamespace(), now=now + timedelta(seconds=2))
        assert (await load_account(db, 1))[0].remaining_bytes(now) == expected_base + 20


async def test_extension_only_stages_an_explicit_reset_decision(monkeypatch):
    from sqlalchemy import select

    from app.database.crud.subscription import extend_subscription
    from app.database.models import TrafficBaseGrantIntent

    now = datetime.now(UTC)
    async with database(monkeypatch) as db:
        sub = await db.get(Subscription, 1)
        sub.traffic_accounting_mode = 'managed'
        sub.traffic_limit_gb = 2
        sub.is_trial = False
        sub.status = 'active'
        sub.end_date = now + timedelta(days=10)
        state = await initialize_account(
            db,
            1,
            TrafficAccount('panel', 0, 0, (Allowance('base:old', 10, now + timedelta(days=30)),)),
            strategy='NO_RESET',
            timezone='UTC',
            anchor=now,
            next_refresh_at=None,
        )
        state.phase = 'ready'
        await db.commit()
        await extend_subscription(db, sub, 1, commit=False)
        assert not (await db.scalars(select(TrafficBaseGrantIntent))).all()
        await extend_subscription(db, sub, 1, reset_used_traffic=False, traffic_limit_gb=2, commit=False)
        assert not (await db.scalars(select(TrafficBaseGrantIntent))).all()
        await extend_subscription(db, sub, 1, reset_used_traffic=True, traffic_limit_gb=2, commit=False)
        assert len((await db.scalars(select(TrafficBaseGrantIntent))).all()) == 1
        await db.rollback()
        assert not (await db.scalars(select(TrafficBaseGrantIntent))).all()
