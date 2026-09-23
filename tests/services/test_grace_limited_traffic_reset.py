"""Грейс по лимиту трафика закрывается, когда трафик сбросился сам.

Репорт: человек упёрся в лимит, получил грейс на 1 ГБ и израсходовал его. На
следующий день панель по расписанию сбросила трафик — подписка в панели снова
работает, но из грейса человека не вывело: в панели его сквад, дата и лимит
(51 ГБ вместо 50), в боте «доступ не работает».

Причина: пока грейс открыт, статус из панели в бота не переносится, а
``user.enabled`` гасится как эхо оверлея. Расход синхронизируется — в боте он
ноль, а статус так и остался LIMITED. ``billing_has_recovered`` требует active.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.database.models import Base, GraceAccessSessionModel, Subscription, User
from app.services.grace_access_service import (
    GraceAccessSession,
    GraceBillingState,
    GracePanelOverlay,
    GracePanelSnapshot,
    GraceReason,
    GraceSessionState,
    traffic_reset_ended_limited_incident,
)


GIB = 1024**3
PANEL_ID = 9001
TARIFF_SQUAD = '11111111-1111-1111-1111-111111111111'
GRACE_SQUAD = '22222222-2222-2222-2222-222222222222'
NOW = datetime.now(UTC)
END_AT = NOW + timedelta(days=4)
GRACE_UNTIL = NOW + timedelta(days=1)


def _billing(**changes) -> GraceBillingState:
    base = GraceBillingState(
        subscription_id=42,
        remnawave_id=PANEL_ID,
        status='limited',
        end_at=END_AT,
        traffic_limit_bytes=50 * GIB,
        used_traffic_bytes=50 * GIB,
        device_limit=3,
        squad_uuids=(TARIFF_SQUAD,),
    )
    return replace(base, **changes)


def _session(reason: GraceReason = GraceReason.LIMITED) -> GraceAccessSession:
    return GraceAccessSession(
        id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        subscription_id=42,
        remnawave_id=PANEL_ID,
        reason=reason,
        incident_key='limited:test',
        state=GraceSessionState.ACTIVE,
        billing_before=_billing(),
        panel_before=GracePanelSnapshot(
            remnawave_id=PANEL_ID,
            status='LIMITED',
            expire_at=END_AT,
            traffic_limit_bytes=50 * GIB,
            used_traffic_bytes=50 * GIB,
            squad_uuids=(TARIFF_SQUAD,),
        ),
        overlay=GracePanelOverlay(
            status='ACTIVE',
            expire_at=GRACE_UNTIL,
            traffic_limit_bytes=51 * GIB,
            squad_uuids=(GRACE_SQUAD,),
        ),
        started_at=NOW - timedelta(days=1),
        grace_until=GRACE_UNTIL,
        updated_at=NOW,
    )


def test_usage_dropped_below_the_limit_means_the_incident_is_over():
    assert traffic_reset_ended_limited_incident(_session(), _billing(used_traffic_bytes=GIB // 10), now=NOW)


def test_grace_traffic_being_spent_is_not_a_reset():
    assert not traffic_reset_ended_limited_incident(_session(), _billing(used_traffic_bytes=51 * GIB), now=NOW)


def test_unchanged_usage_is_not_a_reset():
    assert not traffic_reset_ended_limited_incident(_session(), _billing(), now=NOW)


def test_expired_reason_is_left_to_the_renewal_path():
    assert not traffic_reset_ended_limited_incident(
        _session(GraceReason.EXPIRED), _billing(used_traffic_bytes=0), now=NOW
    )


def test_subscription_that_ran_out_of_time_is_not_reactivated():
    ended = _billing(used_traffic_bytes=0, end_at=NOW - timedelta(minutes=1))

    assert not traffic_reset_ended_limited_incident(_session(), ended, now=NOW)


def test_blocked_user_is_not_reactivated():
    assert not traffic_reset_ended_limited_incident(
        _session(), _billing(used_traffic_bytes=0, user_status='blocked'), now=NOW
    )


def test_overlay_date_imported_into_billing_is_not_a_reset():
    echoed = _billing(used_traffic_bytes=0, end_at=GRACE_UNTIL)

    assert not traffic_reset_ended_limited_incident(_session(), echoed, now=NOW)


def test_changed_limit_is_left_to_the_conflict_path():
    assert not traffic_reset_ended_limited_incident(
        _session(), _billing(used_traffic_bytes=0, traffic_limit_bytes=100 * GIB), now=NOW
    )


@pytest_asyncio.fixture
async def lab(monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.services import grace_access_runtime as rt
    from app.services.grace_access_service import GraceAccessMode
    from tests.fixtures.sqlite_memory import ensure_real_aiosqlite

    ensure_real_aiosqlite(monkeypatch)
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=list(Base.metadata.sorted_tables)))
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with maker() as db:
        db.add(User(id=1, telegram_id=1001, first_name='U', language='ru', status='active', balance_kopeks=0))
        await db.flush()
        db.add(
            Subscription(
                id=42,
                remnawave_short_id='sub42',
                user_id=1,
                status='limited',
                is_trial=False,
                start_date=NOW - timedelta(days=26),
                end_date=END_AT,
                traffic_limit_gb=50,
                traffic_used_gb=50.0,
                device_limit=3,
                connected_squads=[TARIFF_SQUAD],
                remnawave_id=PANEL_ID,
            )
        )
        await db.commit()
        await rt.SQLAlchemyGraceSessionStore(db, subscription_id=42).create(_session())
        await db.commit()

    monkeypatch.setattr(rt, 'AsyncSessionLocal', maker)
    monkeypatch.setattr(rt, 'announce_grace_event', AsyncMock())
    monkeypatch.setattr(rt.settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(rt.settings, 'MULTI_TARIFF_ENABLED', True)

    panel = SimpleNamespace(applied=[])

    class _Panel:
        def __init__(self, **_):
            pass

        async def read_snapshot(self, remnawave_id):
            # Панель под действующим оверлеем грейса.
            return GracePanelSnapshot(
                remnawave_id=PANEL_ID,
                status='ACTIVE',
                expire_at=GRACE_UNTIL,
                traffic_limit_bytes=51 * GIB,
                used_traffic_bytes=GIB // 10,
                squad_uuids=(GRACE_SQUAD,),
            )

        async def apply_billing_state(self, billing, *, expected_overlay=None):
            panel.applied.append(billing)

    monkeypatch.setattr(rt, 'RemnawaveGracePanelGateway', _Panel)
    runtime = rt.GraceAccessRuntime()
    runtime._mode = GraceAccessMode.ACTIVE
    runtime.bot = object()
    try:
        yield SimpleNamespace(rt=rt, runtime=runtime, maker=maker, panel=panel)
    finally:
        await engine.dispose()


async def _state(maker) -> tuple[str, str]:
    async with maker() as db:
        status = (await db.execute(select(Subscription.status).where(Subscription.id == 42))).scalar_one()
        grace = (await db.execute(select(GraceAccessSessionModel.state))).scalar_one()
        return status, grace


@pytest.mark.asyncio
async def test_scheduled_traffic_reset_closes_the_grace_and_restores_the_panel(lab):
    async with lab.maker() as db:
        sub = await db.get(Subscription, 42)
        sub.traffic_used_gb = 0.1  # расход во время грейса синхронизируется, статус — нет
        await db.commit()

    result = await lab.runtime._process_open(42, drain=False, force_restore=False)

    assert result.paid == 1
    assert await _state(lab.maker) == ('active', 'completed')
    assert [b.status for b in lab.panel.applied] == ['active'], 'панели не вернули канонические настройки'
    assert lab.panel.applied[0].traffic_limit_bytes == 50 * GIB


@pytest.mark.asyncio
async def test_grace_keeps_running_while_the_traffic_is_not_reset(lab):
    result = await lab.runtime._process_open(42, drain=False, force_restore=False)

    assert result.paid == 0
    status, grace = await _state(lab.maker)
    assert status == 'limited'
    assert grace != 'completed'
    assert lab.panel.applied == []
