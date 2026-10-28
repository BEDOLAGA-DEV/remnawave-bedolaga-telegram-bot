"""Issue 3341: explicit daily resume must charge once, including overdue active/grace."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.cabinet.routes.subscription_modules import daily
from app.config import settings
from app.database.models import Base, Subscription, Transaction, User
from tests.fixtures.postgres_db import postgres_session
from tests.services.test_daily_resume_traffic_reset import _FakePanelSync, _rows


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('raw_status', ['trial', 'active', 'expired'])
async def test_trial_cannot_be_paid_through_resume(postgres_database, monkeypatch, multi, raw_status):
    from fastapi import HTTPException

    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    panel = _FakePanelSync()
    monkeypatch.setattr(daily, 'SubscriptionService', lambda: panel)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        rows = _rows('NO_RESET', status=raw_status)
        rows[2].is_trial = True
        rows[2].traffic_limit_gb = 2
        end_date = rows[2].end_date
        db.add_all(rows)
        await db.commit()
        user = await db.get(User, 1)
        balance = user.balance_kopeks
        with pytest.raises(HTTPException) as error:
            await daily.toggle_subscription_pause(user=user, db=db, subscription_id=10, paused=False)
        assert error.value.status_code == 400
        await db.rollback()
        sub = await db.get(Subscription, 10, populate_existing=True)
        assert sub.is_trial is True
        assert sub.end_date == end_date
        assert sub.traffic_limit_gb == 2
        assert (await db.get(User, 1, populate_existing=True)).balance_kopeks == balance
        assert not (await db.scalars(select(Transaction))).all()
        assert not panel.calls


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('raw_status', ['active', 'expired', 'disabled', 'limited'])
@pytest.mark.parametrize('grace', [False, True])
async def test_expired_daily_resume_and_retry(postgres_database, monkeypatch, raw_status, grace, multi):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(daily, 'SubscriptionService', lambda: _FakePanelSync())
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        rows = _rows('NO_RESET', status=raw_status)
        rows[0].balance_kopeks = 9000
        rows[2].autopay_enabled = False
        rows[2].grace_session_open = grace
        rows[2].grace_overlay_expire_at = datetime.now(UTC) + timedelta(hours=3) if grace else None
        db.add_all(rows)
        await db.commit()
        user = await db.get(User, 1)
        result = await daily.toggle_subscription_pause(user=user, db=db, subscription_id=10, paused=False)
        assert result['is_paused'] is False
        sub = await db.get(Subscription, 10)
        end = sub.end_date
        assert sub.actual_status == 'active'
        assert sub.autopay_enabled is False
        assert user.balance_kopeks == 8000
        await daily.toggle_subscription_pause(user=user, db=db, subscription_id=10, paused=False)
        assert user.balance_kopeks == 8000
        assert sub.end_date == end
        assert len((await db.scalars(select(Transaction))).all()) == 1


@pytest.mark.parametrize('paused', [False, True])
async def test_restricted_account_cannot_resume_but_can_pause(postgres_database, monkeypatch, paused):
    from fastapi import HTTPException

    panel = _FakePanelSync()
    monkeypatch.setattr(daily, 'SubscriptionService', lambda: panel)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        rows = _rows('NO_RESET', status='expired')
        rows[0].restriction_subscription = True
        db.add_all(rows)
        await db.commit()
        user = await db.get(User, 1)
        balance = user.balance_kopeks
        if paused:
            result = await daily.toggle_subscription_pause(user=user, db=db, subscription_id=10, paused=True)
            assert result['is_paused'] is True
        else:
            with pytest.raises(HTTPException) as error:
                await daily.toggle_subscription_pause(user=user, db=db, subscription_id=10, paused=False)
            assert error.value.status_code == 403
            await db.rollback()
        assert (await db.get(User, 1, populate_existing=True)).balance_kopeks == balance
        assert not (await db.scalars(select(Transaction))).all()
        assert not panel.calls


async def test_failure_does_not_take_money(postgres_database, monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(
        'app.database.crud.transaction.create_transaction', AsyncMock(side_effect=RuntimeError('ledger unavailable'))
    )
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all(_rows('NO_RESET', status='expired'))
        await db.commit()
        user = await db.get(User, 1)
        balance = user.balance_kopeks
        with pytest.raises(RuntimeError):
            await daily.toggle_subscription_pause(user=user, db=db, subscription_id=10, paused=False)
        await db.rollback()
        assert (await db.get(User, 1, populate_existing=True)).balance_kopeks == balance
        assert (await db.get(Subscription, 10, populate_existing=True)).actual_status == 'expired'


async def test_concurrent_resume_charges_once(postgres_database, monkeypatch):
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(daily, 'SubscriptionService', lambda: _FakePanelSync())
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all(_rows('NO_RESET', status='expired'))
        await db.commit()
        factory = async_sessionmaker(db.bind, expire_on_commit=False)

        async def resume():
            async with factory() as session:
                user = await session.get(User, 1)
                return await daily.toggle_subscription_pause(user=user, db=session, subscription_id=10, paused=False)

        results = await asyncio.gather(resume(), resume())
        assert all(result['is_paused'] is False for result in results)
        assert (await db.get(User, 1, populate_existing=True)).balance_kopeks == 99000
        assert len((await db.scalars(select(Transaction))).all()) == 1
        assert (await db.get(Subscription, 10, populate_existing=True)).is_daily_paused is False


@pytest.mark.parametrize('case', ['insufficient', 'free', 'paid_pause'])
async def test_daily_resume_balance_cases(postgres_database, monkeypatch, case):
    from fastapi import HTTPException

    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(daily, 'SubscriptionService', lambda: _FakePanelSync())
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        rows = _rows('NO_RESET', status='expired')
        rows[0].balance_kopeks = 0
        if case == 'free':
            rows[1].daily_price_kopeks = 0
        if case == 'paid_pause':
            rows[2].status = 'active'
            rows[2].end_date = datetime.now(UTC) + timedelta(days=2)
            rows[2].is_daily_paused = True
        before = rows[2].end_date
        db.add_all(rows)
        await db.commit()
        user = await db.get(User, 1)
        if case == 'insufficient':
            with pytest.raises(HTTPException) as error:
                await daily.toggle_subscription_pause(user=user, db=db, subscription_id=10, paused=False)
            assert error.value.status_code == 402
            await db.rollback()
        else:
            await daily.toggle_subscription_pause(user=user, db=db, subscription_id=10, paused=False)
        sub = await db.get(Subscription, 10, populate_existing=True)
        if case in {'insufficient', 'paid_pause'}:
            assert sub.end_date == before
        else:
            assert sub.actual_status == 'active'
        assert (await db.get(User, 1, populate_existing=True)).balance_kopeks == 0
        assert not (await db.scalars(select(Transaction))).all()
