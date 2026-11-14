"""Paid traffic can recover limited grace without selling its temporary quota."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.crud.user import subtract_user_balance
from app.database.models import Base, GraceAccessSessionModel, Subscription, User
from app.external.remnawave_api import UserStatus
from app.services import grace_access_runtime as rt
from app.services.grace_access_service import (
    GraceAccessMode,
    GraceAccessSession,
    GraceBillingState,
    GracePanelOverlay,
    GracePanelSnapshot,
    GraceReason,
    GraceSessionState,
)
from app.services.traffic_accounting import TrafficAccount
from app.services.traffic_accounting_service import read_counters
from app.services.traffic_accounting_store import initialize_account, load_account
from app.services.traffic_purchase_service import add_subscription_traffic, prepare_traffic_purchase
from tests.fixtures.postgres_db import postgres_sessions
from tests.services.test_grace_access_runtime import FakeRemnawaveApi, make_panel_user


pytestmark = pytest.mark.postgres
GIB = 1024**3
REGULAR = '11111111-1111-1111-1111-111111111111'
GRACE = '22222222-2222-2222-2222-222222222222'


class Panel(FakeRemnawaveApi):
    async def get_user_devices_all(self, user_id):
        return {'total': 0, 'devices': []}

    async def update_user(self, **kwargs):
        result = await super().update_user(**kwargs)
        if 'traffic_limit_strategy' in kwargs:
            result.traffic_limit_strategy = kwargs['traffic_limit_strategy']
        return result


async def seed(db, monkeypatch, managed):
    now = datetime.now(UTC)
    end = now + timedelta(days=10)
    overlay_end = now + timedelta(days=1)
    monkeypatch.setattr(rt.grace_access_runtime, '_mode', GraceAccessMode.ACTIVE)
    monkeypatch.setattr(rt, 'announce_grace_event', AsyncMock())
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    user = User(id=1, telegram_id=1, status='active', balance_kopeks=1000)
    sub = Subscription(
        id=1,
        user_id=1,
        remnawave_id=4242,
        status='limited',
        is_trial=False,
        start_date=now - timedelta(days=20),
        end_date=end,
        traffic_limit_gb=10,
        traffic_used_gb=10,
        connected_squads=[REGULAR],
    )
    db.add_all([user, sub])
    await db.commit()
    await rt.SQLAlchemyGraceSessionStore(db, subscription_id=1).create(
        GraceAccessSession(
            id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            subscription_id=1,
            remnawave_id=4242,
            reason=GraceReason.LIMITED,
            incident_key='limited:topup',
            state=GraceSessionState.ACTIVE,
            billing_before=GraceBillingState(
                subscription_id=1,
                remnawave_id=4242,
                status='limited',
                end_at=end,
                traffic_limit_bytes=10 * GIB,
                used_traffic_bytes=10 * GIB,
                device_limit=2,
                squad_uuids=(REGULAR,),
            ),
            panel_before=GracePanelSnapshot(
                remnawave_id=4242,
                status='LIMITED',
                expire_at=end,
                traffic_limit_bytes=10 * GIB,
                used_traffic_bytes=10 * GIB,
                squad_uuids=(REGULAR,),
            ),
            overlay=GracePanelOverlay(
                status='ACTIVE', expire_at=overlay_end, traffic_limit_bytes=11 * GIB, squad_uuids=(GRACE,)
            ),
            started_at=now,
            grace_until=overlay_end,
            updated_at=now,
        )
    )
    panel = make_panel_user(
        status=UserStatus.ACTIVE, expire_at=overlay_end, traffic_limit_bytes=11 * GIB, squad_uuids=(GRACE,)
    )
    panel.id = 4242
    panel.created_at = now - timedelta(days=20)
    panel.traffic_limit_strategy = 'NO_RESET' if managed else 'MONTH'
    panel.user_traffic = SimpleNamespace(
        used_traffic_bytes=10 * GIB + 12345, lifetime_used_traffic_bytes=20 * GIB + 12345
    )
    panel.used_traffic_bytes = panel.user_traffic.used_traffic_bytes
    api = Panel(panel)

    @asynccontextmanager
    async def client(_self):
        yield api

    monkeypatch.setattr('app.services.subscription_service.SubscriptionService.get_api_client', client)
    # Delivery is exercised explicitly, so background tasks cannot race the assertions.
    monkeypatch.setattr('app.services.traffic_accounting_service.schedule_projection_after_commit', lambda *a: None)
    if managed:
        counters = read_counters(panel)
        state = await initialize_account(
            db,
            1,
            TrafficAccount(counters.identity, counters.lifetime_bytes, counters.used_bytes, ()),
            strategy='MONTH',
            timezone='UTC',
            anchor=panel.created_at,
            next_refresh_at=now + timedelta(days=5),
        )
        state.phase = 'ready'
        state.grace_excluded = True
        state.quota_disabled = True
        sub.traffic_accounting_mode = 'managed'
    await db.commit()
    return sub, user, api


@pytest.mark.parametrize('managed', [False, True])
@pytest.mark.parametrize('rollback', [False, True])
@pytest.mark.parametrize('reset_counter', [False, True])
async def test_topup_preserves_paid_period_and_excludes_grace_usage(
    postgres_database, monkeypatch, managed, rollback, reset_counter
):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, observer):
        sub, user, api = await seed(db, monkeypatch, managed)
        end = sub.end_date
        if reset_counter:
            api.user.user_traffic.used_traffic_bytes = 0
            api.user.traffic_limit_bytes = GIB
        sub, panel = await prepare_traffic_purchase(db, sub, requested_gb=5)
        assert await subtract_user_balance(db, user, 100, 'test traffic', commit=False)
        await add_subscription_traffic(db, sub, 5, panel=panel, commit=False)
        if rollback:
            await db.rollback()
        else:
            await db.commit()
        await observer.refresh(await observer.get(Subscription, 1))
        stored = await observer.get(Subscription, 1)
        assert stored.end_date == end
        assert stored.traffic_limit_gb == (10 if rollback else 15)
        assert (await observer.get(User, 1)).balance_kopeks == (1000 if rollback else 900)
        account, state = await load_account(observer, 1)
        assert account.remaining_bytes(datetime.now(UTC)) == (0 if rollback else 5 * GIB)
        assert state.grace_excluded
        assert stored.grace_session_open
        assert api.user.active_internal_squads == [{'uuid': GRACE}]


@pytest.mark.parametrize('managed', [False, True])
async def test_projection_closes_grace_and_keeps_full_new_package(postgres_database, monkeypatch, managed):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services.traffic_accounting_delivery import deliver_account_projection

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, observer):
        sub, user, api = await seed(db, monkeypatch, managed)
        end = sub.end_date
        sub, panel = await prepare_traffic_purchase(db, sub, requested_gb=5)
        assert await subtract_user_balance(db, user, 100, 'test traffic', commit=False)
        await add_subscription_traffic(db, sub, 5, panel=panel, commit=False)
        await db.commit()
        # Traffic consumed after payment but before overlay exit is still grace.
        api.user.user_traffic.used_traffic_bytes += 5678
        api.user.user_traffic.lifetime_used_traffic_bytes += 5678
        await deliver_account_projection(async_sessionmaker(db.bind, expire_on_commit=False), api, 1)
        stored = await observer.get(Subscription, 1)
        session = await observer.get(GraceAccessSessionModel, 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')
        account, state = await load_account(observer, 1)
        assert stored.end_date == end
        assert stored.status == 'active'
        assert not stored.grace_session_open
        assert session.state == 'completed'
        assert session.completion_reason == 'paid'
        assert not state.grace_excluded
        assert account.remaining_bytes(datetime.now(UTC)) == 5 * GIB
        assert api.user.expire_at == end
        assert api.user.active_internal_squads == [{'uuid': REGULAR}]
        assert api.user.traffic_limit_bytes == api.user.user_traffic.used_traffic_bytes + 5 * GIB


@pytest.mark.parametrize('managed', [False, True])
@pytest.mark.parametrize(
    'case', ['expired', 'disabled', 'trial', 'blocked', 'expired_grace', 'pending', 'unknown_usage']
)
async def test_ineligible_grace_never_charges_or_grants(postgres_database, monkeypatch, managed, case):
    from app.services.traffic_accounting import AccountingConflict

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, observer):
        sub, user, api = await seed(db, monkeypatch, managed)
        session = await db.get(GraceAccessSessionModel, 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')
        if case == 'expired':
            sub.end_date = datetime.now(UTC) - timedelta(seconds=1)
        elif case == 'disabled':
            sub.status = 'disabled'
        elif case == 'trial':
            sub.is_trial = True
        elif case == 'blocked':
            user.status = 'blocked'
        elif case == 'expired_grace':
            session.reason = 'expired'
        elif case == 'pending':
            session.state = 'pending'
        elif case == 'unknown_usage':
            if managed:
                session.state = 'restoring'
            session.panel_before = {**session.panel_before, 'traffic_is_known': False}
        await db.commit()
        with pytest.raises(AccountingConflict):
            await prepare_traffic_purchase(db, sub, requested_gb=5)
        await db.rollback()
        assert (await observer.get(User, 1)).balance_kopeks == 1000
        assert (await observer.get(Subscription, 1)).traffic_limit_gb == 10
        assert api.updates == []


@pytest.mark.parametrize('managed', [False, True])
async def test_failed_panel_delivery_retries_without_second_charge(postgres_database, monkeypatch, managed):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services.traffic_accounting_delivery import deliver_account_projection

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, observer):
        sub, user, api = await seed(db, monkeypatch, managed)
        sub, panel = await prepare_traffic_purchase(db, sub, requested_gb=5)
        assert await subtract_user_balance(db, user, 100, 'test traffic', commit=False)
        await add_subscription_traffic(db, sub, 5, panel=panel, commit=False)
        await db.commit()
        maker = async_sessionmaker(db.bind, expire_on_commit=False)
        with monkeypatch.context() as patch:
            patch.setattr(api, 'update_user', AsyncMock(side_effect=TimeoutError('panel unavailable')))
            with pytest.raises(TimeoutError):
                await deliver_account_projection(maker, api, 1)
        session = await observer.get(GraceAccessSessionModel, 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')
        assert session.state == 'active'
        assert (await observer.get(User, 1)).balance_kopeks == 900
        assert (await load_account(observer, 1))[0].remaining_bytes(datetime.now(UTC)) == 5 * GIB
        await observer.rollback()
        await deliver_account_projection(maker, api, 1)
        assert (await observer.get(User, 1)).balance_kopeks == 900
        assert (
            await observer.get(GraceAccessSessionModel, 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')
        ).state == 'completed'


@pytest.mark.parametrize('managed', [False, True])
@pytest.mark.parametrize('refusal', [False, True])
async def test_cabinet_topup_returns_definite_result(postgres_database, monkeypatch, managed, refusal):
    from fastapi import HTTPException

    from app.cabinet.routes.subscription_modules.traffic import purchase_traffic
    from app.cabinet.schemas.subscription import TrafficPurchaseRequest
    from app.database.models import Tariff
    from app.services.subscription_service import SubscriptionService

    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', AsyncMock())
    monkeypatch.setattr(SubscriptionService, 'enable_remnawave_user', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, observer):
        sub, user, api = await seed(db, monkeypatch, managed)
        db.add(
            Tariff(
                id=1,
                name='Test',
                traffic_limit_gb=10,
                traffic_topup_enabled=True,
                traffic_topup_packages={'5': 100},
                period_prices={'30': 1000},
            )
        )
        await db.flush()
        sub.tariff_id = 1
        sub.remnawave_short_id = 'masked-1'
        await db.commit()
        if refusal:
            monkeypatch.setattr(api, 'get_user_by_id', AsyncMock(side_effect=TimeoutError('panel unavailable')))
            with pytest.raises(HTTPException) as exc:
                await purchase_traffic(TrafficPurchaseRequest(gb=5), user=user, db=db, subscription_id=1)
            assert exc.value.status_code == 409
            assert 'No funds were charged' in exc.value.detail
        else:
            result = await purchase_traffic(TrafficPurchaseRequest(gb=5), user=user, db=db, subscription_id=1)
            assert result['success']
            assert result['gb_added'] == 5
        assert (await observer.get(User, 1)).balance_kopeks == (1000 if refusal else 900)
        assert (await observer.get(Subscription, 1)).traffic_limit_gb == (10 if refusal else 15)
