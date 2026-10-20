"""#3167: switching must resolve one owned subscription before charging."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.routes.subscription_modules import tariff_switch as route
from app.cabinet.schemas.subscription import TariffPurchaseRequest
from app.config import settings
from app.database.models import Base, Subscription, Tariff, Transaction, User
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@asynccontextmanager
async def switch_data(postgres_database, monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_UPGRADE_ENABLED', True)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_DOWNGRADE_ENABLED', True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(route, 'SubscriptionService', lambda: SimpleNamespace(create_remnawave_user=AsyncMock()))
    now = datetime.now(UTC)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user = User(id=1, telegram_id=1, first_name='Test', language='ru', status='active', balance_kopeks=10000)
        foreign = User(id=2, telegram_id=2, first_name='Other', language='ru', status='active')
        db.add_all([user, foreign])
        db.add_all(
            [
                Tariff(
                    id=i,
                    name=f'Tariff {i}',
                    is_active=True,
                    traffic_limit_gb=100 * i,
                    device_limit=2,
                    period_prices={'30': 1000 * i},
                    allowed_squads=[],
                )
                for i in [1, 2, 3]
            ]
        )
        db.add_all(
            [
                Subscription(
                    id=i,
                    user_id=owner,
                    tariff_id=tariff,
                    remnawave_short_id=f'test-{i}',
                    is_trial=False,
                    status='active',
                    start_date=now - timedelta(days=10),
                    end_date=now + timedelta(days=days, hours=12),
                    traffic_limit_gb=100 * tariff,
                    device_limit=2,
                    connected_squads=[],
                )
                for i, owner, tariff, days in [(11, 1, 1, 10), (12, 1, 2, 15), (21, 2, 1, 10)]
            ]
        )
        await db.commit()
        yield db, user


@pytest.mark.parametrize('action', ['preview', 'switch'])
@pytest.mark.parametrize('selected', [None, 0, -1, 21, 999])
async def test_multi_switch_rejects_ambiguous_or_foreign_subscription(postgres_database, monkeypatch, action, selected):
    async with switch_data(postgres_database, monkeypatch) as (db, user):
        handler = route.preview_tariff_switch if action == 'preview' else route.switch_tariff
        with pytest.raises(HTTPException) as exc:
            await handler(TariffPurchaseRequest(tariff_id=3, period_days=30), user, db, selected)
        assert exc.value.status_code == (400 if selected is None else 404)
        if selected is None:
            assert exc.value.detail['code'] == 'subscription_selection_required'
        await db.refresh(user)
        assert user.balance_kopeks == 10000
        assert list(
            (await db.execute(select(Subscription.id, Subscription.tariff_id).order_by(Subscription.id))).all()
        ) == [(11, 1), (12, 2), (21, 1)]
        assert (await db.scalars(select(Transaction))).all() == []


@pytest.mark.parametrize('selected', [11, 12, None])
@pytest.mark.parametrize('direction', ['upgrade', 'downgrade'])
@pytest.mark.parametrize('enabled', [False, True])
async def test_switch_updates_only_the_selected_subscription(
    postgres_database, monkeypatch, selected, direction, enabled
):
    async with switch_data(postgres_database, monkeypatch) as (db, user):
        source_id = selected or 11
        other_id = 12 if source_id == 11 else 11
        source = await db.get(Subscription, source_id)
        other = await db.get(Subscription, other_id)
        if selected is None:
            other.status = 'disabled'
        # Separate unused target tariff permits both directions without violating
        # the existing one-alive-subscription-per-tariff policy.
        target = await db.get(Tariff, 3)
        if direction == 'downgrade':
            target.period_prices = {'30': 300}
        monkeypatch.setattr(settings, f'TARIFF_SWITCH_{direction.upper()}_ENABLED', enabled)
        await db.commit()
        other_before = (other.tariff_id, other.end_date, other.traffic_limit_gb, other.device_limit)
        foreign_before = (await db.get(Subscription, 21)).tariff_id
        request = TariffPurchaseRequest(tariff_id=3, period_days=30)
        if not enabled:
            for handler in [route.preview_tariff_switch, route.switch_tariff]:
                with pytest.raises(HTTPException) as exc:
                    await handler(request, user, db, selected)
                assert exc.value.status_code == 403
            await db.refresh(user)
            assert user.balance_kopeks == 10000
            assert source.tariff_id != 3
            assert (await db.scalars(select(Transaction))).all() == []
            return
        preview = await route.preview_tariff_switch(request, user, db, selected)
        assert preview['current_tariff_id'] == source.tariff_id
        expected_charge = 0 if direction == 'downgrade' else 500 if source_id == 12 else 666
        assert preview['upgrade_cost_kopeks'] == expected_charge
        result = await route.switch_tariff(request, user, db, selected)
        await db.refresh(source)
        await db.refresh(other)
        await db.refresh(user)
        assert source.tariff_id == 3
        assert source.traffic_limit_gb == 300
        assert source.device_limit == 2
        assert result['subscription'].id == source_id
        assert (other.tariff_id, other.end_date, other.traffic_limit_gb, other.device_limit) == other_before
        assert (await db.get(Subscription, 21)).tariff_id == foreign_before
        assert result['charged_kopeks'] == preview['upgrade_cost_kopeks']
        assert user.balance_kopeks == 10000 - result['charged_kopeks']
        transactions = (await db.scalars(select(Transaction))).all()
        assert len(transactions) == 1
        assert abs(transactions[0].amount_kopeks) == result['charged_kopeks']
        # Retrying an already completed switch must not charge again.
        with pytest.raises(HTTPException) as exc:
            await route.switch_tariff(request, user, db, source_id)
        assert exc.value.status_code == 400
        assert len((await db.scalars(select(Transaction))).all()) == 1


async def test_concurrent_switch_of_selected_subscription_charges_once(postgres_database, monkeypatch):
    async with switch_data(postgres_database, monkeypatch) as (db, user):
        resolve = route._resolve_switch_subscription
        both_resolved = asyncio.Event()
        resolved_count = 0

        async def barrier_resolve(session, owner, subscription_id):
            nonlocal resolved_count
            sub = await resolve(session, owner, subscription_id)
            resolved_count += 1
            if resolved_count == 2:
                both_resolved.set()
            await asyncio.wait_for(both_resolved.wait(), timeout=5)
            return sub

        monkeypatch.setattr(route, '_resolve_switch_subscription', barrier_resolve)
        async with (
            AsyncSession(db.bind, expire_on_commit=False, autoflush=False) as first,
            AsyncSession(db.bind, expire_on_commit=False, autoflush=False) as second,
        ):

            async def switch(session):
                owner = await session.get(User, user.id)
                try:
                    return await route.switch_tariff(
                        TariffPurchaseRequest(tariff_id=3, period_days=30), owner, session, 11
                    )
                finally:
                    await session.rollback()

            results = await asyncio.wait_for(asyncio.gather(switch(first), switch(second), return_exceptions=True), 15)
        assert len([result for result in results if isinstance(result, dict)]) == 1
        errors = [result for result in results if isinstance(result, HTTPException)]
        assert len(errors) == 1 and errors[0].status_code == 400
        await db.refresh(user)
        assert user.balance_kopeks == 9334
        transactions = (await db.scalars(select(Transaction))).all()
        assert len(transactions) == 1 and abs(transactions[0].amount_kopeks) == 666
        assert list(
            (await db.execute(select(Subscription.id, Subscription.tariff_id).order_by(Subscription.id))).all()
        ) == [(11, 3), (12, 2), (21, 1)]
