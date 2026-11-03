"""The accounting store participates in the caller's transaction."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database.models import Base, Subscription, TrafficAccountingState, User
from app.services.traffic_accounting import AccountingConflict, Allowance, TrafficAccount, observe_usage
from app.services.traffic_accounting_store import initialize_account, load_account, save_account
from tests.fixtures.sqlite_memory import memory_session


NOW = datetime(2026, 1, 1, tzinfo=UTC)


@asynccontextmanager
async def database(monkeypatch):
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as session:
        session.add(User(id=1))
        session.add(Subscription(id=1, user_id=1, start_date=NOW, end_date=NOW + timedelta(days=30)))
        await session.commit()
        yield session


def initial():
    return TrafficAccount('123', 0, 0, (Allowance('base:jan', 100, NOW + timedelta(days=30)),))


async def initialize(db):
    return await initialize_account(
        db,
        1,
        initial(),
        strategy='MONTH',
        timezone='UTC',
        anchor=NOW,
        next_refresh_at=NOW + timedelta(days=31),
    )


async def test_save_and_reload_preserves_watermarks_and_grant_keys(monkeypatch):
    async with database(monkeypatch) as db:
        state = await initialize(db)
        updated = observe_usage(initial(), '123', 30, 30, NOW)
        revision = await save_account(db, 1, updated, expected_revision=state.revision)
        await db.commit()
        reloaded, stored = await load_account(db, 1)
        assert reloaded == updated
        assert stored.revision == revision == 1
        assert stored.phase == 'prepared'  # DB creation must not claim the panel was switched.


async def test_stale_revision_cannot_overwrite_consumed_traffic(monkeypatch):
    async with database(monkeypatch) as db:
        state = await initialize(db)
        await save_account(db, 1, observe_usage(initial(), '123', 50, 50, NOW), expected_revision=state.revision)
        with pytest.raises(AccountingConflict):
            await save_account(db, 1, initial(), expected_revision=0)


async def test_rollback_does_not_leave_an_account_or_grant(monkeypatch):
    async with database(monkeypatch) as db:
        await initialize(db)
        await db.rollback()
        assert (await db.scalars(select(TrafficAccountingState))).first() is None


async def test_bootstrap_replay_does_not_restore_spent_allowance(monkeypatch):
    async with database(monkeypatch) as db:
        state = await initialize(db)
        await save_account(db, 1, observe_usage(initial(), '123', 50, 50, NOW), expected_revision=state.revision)
        await db.commit()
        await initialize(db)
        loaded, _ = await load_account(db, 1)
        assert loaded.remaining_bytes(NOW) == 50


async def test_store_cannot_restore_spent_grant_even_with_current_revision(monkeypatch):
    async with database(monkeypatch) as db:
        state = await initialize(db)
        revision = await save_account(
            db, 1, observe_usage(initial(), '123', 50, 50, NOW), expected_revision=state.revision
        )
        forged = TrafficAccount('123', 50, 50, initial().allowances)
        with pytest.raises(AccountingConflict, match='Spent grant'):
            await save_account(db, 1, forged, expected_revision=revision)


async def test_history_cannot_be_deleted_to_replay_an_operation(monkeypatch):
    async with database(monkeypatch) as db:
        state = await initialize(db)
        with pytest.raises(AccountingConflict, match='history'):
            await save_account(db, 1, TrafficAccount('123', 0, 0, ()), expected_revision=state.revision)


async def test_store_rejects_impossible_counter_growth(monkeypatch):
    async with database(monkeypatch) as db:
        state = await initialize(db)
        revision = await save_account(
            db, 1, TrafficAccount('123', 100, 50, initial().allowances), expected_revision=state.revision
        )
        with pytest.raises(AccountingConflict, match='counter'):
            await save_account(db, 1, TrafficAccount('123', 100, 100, initial().allowances), expected_revision=revision)
