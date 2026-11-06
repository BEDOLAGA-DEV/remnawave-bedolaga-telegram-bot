"""Paid grace recovery must validate the effective surviving squad target."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import GraceAccessSessionModel, Subscription, Tariff, User
from app.external.remnawave_api import RemnaWaveAPIError, UserStatus
from app.services import grace_access_runtime as runtime
from app.services.grace_access_service import GraceAccessMode, GracePanelSnapshot, GraceReason
from tests.fixtures.postgres_db import postgres_session
from tests.fixtures.sqlite_memory import memory_session
from tests.services.test_grace_access_runtime import (
    OTHER_SQUAD,
    PANEL_ID,
    REGULAR_SQUAD,
    FakeRemnawaveApi,
    make_panel_user,
)
from tests.services.test_grace_access_service import FakePanelGateway


@pytest.fixture
def grace_database_dsn(request, database):
    return request.getfixturevalue('postgres_database') if database == 'postgres' else None


@pytest.mark.parametrize('database', ['sqlite', pytest.param('postgres', marks=pytest.mark.postgres)])
@pytest.mark.parametrize(('has_survivor', 'wrong_response'), [(True, False), (False, False), (True, True)])
@pytest.mark.parametrize('multi', [True, False])
async def test_paid_grace_completion_after_deleted_squad(
    monkeypatch, grace_database_dsn, has_survivor, multi, database, wrong_response
):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'GRACE_ACCESS_EXPIRED_SQUAD_UUID', '11111111-1111-1111-1111-111111111111')
    monkeypatch.setattr(settings, 'GRACE_ACCESS_RESET_TRAFFIC_ON_START', False)
    monkeypatch.setattr(runtime.grace_access_runtime, '_mode', GraceAccessMode.ACTIVE)
    now = datetime.now(UTC)
    assigned = [REGULAR_SQUAD, OTHER_SQUAD] if has_survivor else [OTHER_SQUAD]
    tables = [User.__table__, Subscription.__table__, Tariff.__table__, GraceAccessSessionModel.__table__]
    session = (
        postgres_session(grace_database_dsn, tables) if database == 'postgres' else memory_session(monkeypatch, tables)
    )
    async with session as db:
        user = User(id=42, telegram_id=42, status='active', remnawave_id=PANEL_ID if not multi else None)
        sub = Subscription(
            id=42,
            user_id=42,
            status='expired',
            is_trial=False,
            start_date=now - timedelta(days=31),
            end_date=now - timedelta(days=1),
            remnawave_id=PANEL_ID if multi else None,
            connected_squads=assigned,
            traffic_limit_gb=10,
            traffic_used_gb=3,
            device_limit=2,
        )
        db.add_all([user, sub])
        await db.commit()
        panel = FakePanelGateway(
            GracePanelSnapshot(
                remnawave_id=PANEL_ID,
                status='EXPIRED',
                expire_at=sub.end_date,
                traffic_limit_bytes=10 * 1024**3,
                used_traffic_bytes=3 * 1024**3,
                squad_uuids=tuple(assigned),
            )
        )
        monkeypatch.setattr(runtime, 'RemnawaveGracePanelGateway', lambda **kwargs: panel)
        billing = await runtime.SQLAlchemyGraceBillingGateway(db).get_subscription(42)
        await runtime._build_core(db, subscription_id=42).start_if_eligible(billing, GraceReason.EXPIRED)
        await db.commit()
        sub.end_date = now + timedelta(days=30)
        sub.status = 'active'
        await db.commit()
        paid_end = sub.end_date

        class DeletedSquadAPI(FakeRemnawaveApi):
            async def get_internal_squads(self):
                return [SimpleNamespace(uuid=REGULAR_SQUAD)]

            async def update_user(self, **kwargs):
                if OTHER_SQUAD in kwargs.get('active_internal_squads', []):
                    raise RemnaWaveAPIError('Update user error', 500, {'errorCode': 'A039'})
                updated = await super().update_user(**kwargs)
                if wrong_response:
                    updated.active_internal_squads = [{'uuid': 'unexpected'}]
                return updated

        api = DeletedSquadAPI(
            make_panel_user(
                status=UserStatus.ACTIVE,
                expire_at=now + timedelta(days=3),
                traffic_limit_bytes=4 * 1024**3,
                squad_uuids=('11111111-1111-1111-1111-111111111111',),
            )
        )
        await runtime.lock_grace_sensitive_panel_updates(db, (42,))
        if has_survivor and not wrong_response:
            completed, updated = await runtime.apply_recovered_grace_update_locked(
                db, api, 42, update_kwargs={'user_id': PANEL_ID}, source='test'
            )
            assert completed is True
            assert updated.active_internal_squads == [{'uuid': REGULAR_SQUAD}]
            assert updated.traffic_limit_bytes == 10 * 1024**3
            assert updated.expire_at == paid_end
        else:
            with pytest.raises(runtime.GracePanelError if wrong_response else RemnaWaveAPIError):
                await runtime.apply_recovered_grace_update_locked(
                    db, api, 42, update_kwargs={'user_id': PANEL_ID}, source='test'
                )
        await db.commit()
        row = (await db.scalars(select(GraceAccessSessionModel))).one()
        assert row.state == ('completed' if has_survivor and not wrong_response else 'active')
        await db.refresh(sub)
        assert sub.connected_squads == assigned
        assert sub.end_date.replace(tzinfo=UTC) == paid_end


async def test_background_paid_reconciliation_checks_surviving_target(monkeypatch):
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from tests.services.test_grace_access_runtime import install_fake_api, make_limited_billing, make_overlay

    billing = replace(
        make_limited_billing(), status='active', used_traffic_bytes=0, squad_uuids=(REGULAR_SQUAD, OTHER_SQUAD)
    )
    overlay = make_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.ACTIVE,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
        )
    )
    update = api.update_user

    async def reject_deleted(**kwargs):
        if OTHER_SQUAD in kwargs.get('active_internal_squads', []):
            raise RemnaWaveAPIError('Update user error', 500, {'errorCode': 'A039'})
        return await update(**kwargs)

    api.update_user = reject_deleted
    api.get_internal_squads = AsyncMock(return_value=[SimpleNamespace(uuid=REGULAR_SQUAD)])
    install_fake_api(monkeypatch, api)
    await runtime.RemnawaveGracePanelGateway().apply_billing_state(billing, expected_overlay=overlay)
    assert api.user.active_internal_squads == [{'uuid': REGULAR_SQUAD}]
    assert api.user.traffic_limit_bytes == billing.traffic_limit_bytes
    assert api.user.expire_at == billing.end_at
    assert api.user.hwid_device_limit == billing.device_limit
