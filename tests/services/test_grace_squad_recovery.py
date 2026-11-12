"""Recovery must prove deletion and preserve operator revocations."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.external.remnawave_api import UserStatus
from app.services.grace_access_service import (
    GraceAccessSession,
    GraceBillingState,
    GracePanelOverlay,
    GracePanelSnapshot,
    GraceReason,
    GraceSessionState,
    GraceSquadReplacementRequired,
)
from app.services.grace_squad_recovery import overlay_after_confirmed_deletion, repair_deleted_grace_squad
from tests.services.test_grace_access_runtime import PANEL_ID, FakeRemnawaveApi, make_panel_user


OLD = '11111111-1111-1111-1111-111111111111'
NEW = '22222222-2222-2222-2222-222222222222'


def setup():
    now = datetime.now(UTC)
    deadline = now + timedelta(hours=3)
    overlay = GracePanelOverlay(
        status='ACTIVE', expire_at=deadline, traffic_limit_bytes=1024**3, squad_uuids=(OLD,), reset_used_traffic=True
    )
    snapshot = GracePanelSnapshot(
        remnawave_id=PANEL_ID,
        status='EXPIRED',
        expire_at=now - timedelta(days=1),
        traffic_limit_bytes=10 * 1024**3,
        used_traffic_bytes=0,
        squad_uuids=(NEW,),
    )
    session = GraceAccessSession(
        id='test',
        subscription_id=42,
        remnawave_id=PANEL_ID,
        reason=GraceReason.EXPIRED,
        incident_key='test',
        state=GraceSessionState.ACTIVE,
        billing_before=GraceBillingState(
            subscription_id=42,
            remnawave_id=PANEL_ID,
            status='expired',
            end_at=snapshot.expire_at,
            traffic_limit_bytes=10 * 1024**3,
            used_traffic_bytes=0,
            device_limit=1,
            squad_uuids=(NEW,),
        ),
        panel_before=snapshot,
        overlay=overlay,
        started_at=now,
        grace_until=deadline,
        updated_at=now,
    )
    panel = make_panel_user(
        status=UserStatus.ACTIVE, expire_at=deadline, traffic_limit_bytes=overlay.traffic_limit_bytes, squad_uuids=()
    )
    api = FakeRemnawaveApi(panel)
    api.get_internal_squads = AsyncMock(return_value=[SimpleNamespace(uuid=NEW)])
    return session, api


@pytest.mark.parametrize('case', ['old_exists', 'manual_disable', 'changed_limit', 'changed_expiry', 'foreign_squad'])
async def test_unrelated_changes_never_get_replaced(case):
    session, api = setup()
    if case == 'old_exists':
        api.get_internal_squads.return_value.append(SimpleNamespace(uuid=OLD))
    elif case == 'manual_disable':
        api.user.status = UserStatus.DISABLED
    elif case == 'changed_limit':
        api.user.traffic_limit_bytes += 1
    elif case == 'changed_expiry':
        api.user.expire_at += timedelta(minutes=1)
    elif case == 'foreign_squad':
        api.user.active_internal_squads = [{'uuid': '33333333-3333-3333-3333-333333333333'}]
    assert await repair_deleted_grace_squad(api, session, NEW) == session.overlay
    assert api.updates == []


@pytest.mark.parametrize('case', ['timeout', 'malformed', 'no_replacement'])
async def test_unknown_catalog_or_missing_replacement_never_mutates_panel(case):
    session, api = setup()
    expected = GraceSquadReplacementRequired
    if case == 'timeout':
        api.get_internal_squads.side_effect = TimeoutError()
        expected = TimeoutError
    elif case == 'malformed':
        api.get_internal_squads.return_value = [{}]
        expected = ValueError
    else:
        api.get_internal_squads.return_value = []
    with pytest.raises(expected):
        await repair_deleted_grace_squad(api, session, NEW)
    assert api.updates == []


async def test_active_repair_only_changes_squads():
    session, api = setup()
    changed = await repair_deleted_grace_squad(api, session, NEW)
    assert changed == replace(session.overlay, squad_uuids=(NEW,))
    assert api.updates == [{'user_id': PANEL_ID, 'active_internal_squads': [NEW], 'external_squad_uuid': None}]


@pytest.mark.parametrize('deleted', [False, True])
async def test_teardown_accepts_only_catalog_proven_deletion(deleted):
    from app.services.grace_access_runtime import _panel_user_to_snapshot

    session, api = setup()
    if not deleted:
        api.get_internal_squads.return_value.append(SimpleNamespace(uuid=OLD))
    current = _panel_user_to_snapshot(api.user)
    target = await overlay_after_confirmed_deletion(api, current, session.overlay, now=datetime.now(UTC))
    assert target.squad_uuids == (() if deleted else (OLD,))
    assert api.updates == []


async def test_unchanged_missing_squad_warning_is_not_repeated(monkeypatch):
    from unittest.mock import Mock

    from app.services import grace_access_service as core
    from tests.services.test_grace_access_service import MutableClock, make_billing, make_service, make_snapshot

    now = datetime.now(UTC)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    service, store, panel, _ = make_service(
        billing=billing, snapshot=make_snapshot(expire_at=billing.end_at), clock=MutableClock(now)
    )
    panel.fail_overlay_attempts = 1
    with pytest.raises(RuntimeError):
        await service.start_if_eligible(billing, GraceReason.EXPIRED)
    panel.repair_deleted_squad = AsyncMock(side_effect=GraceSquadReplacementRequired('Select replacement grace squad'))
    warning = Mock()
    monkeypatch.setattr(core.logger, 'warning', warning)
    first = await service.reconcile()
    second = await service.reconcile()
    assert first.errors == second.errors == 1
    assert 'Select replacement grace squad' in store.only_session().last_error
    assert warning.call_count == 1
