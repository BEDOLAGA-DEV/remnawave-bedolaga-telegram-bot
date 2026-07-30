from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.external.remnawave_api import UserStatus
from app.services.grace_access_runtime import (
    RemnawaveGracePanelGateway,
    _PanelTarget,
    _serialize_panel_target,
)
from app.services.grace_access_service import (
    GraceBillingState,
    GracePanelOverlay,
    GracePanelSnapshot,
    GracePanelTransitionConflict,
    GracePanelTransitionPending,
    GraceRestoreOutcome,
)


GIB = 1024**3
PANEL_UUID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
GRACE_SQUAD = '11111111-1111-1111-1111-111111111111'
REGULAR_SQUAD = '22222222-2222-2222-2222-222222222222'
OTHER_SQUAD = '33333333-3333-3333-3333-333333333333'
EXTERNAL_SQUAD = '44444444-4444-4444-4444-444444444444'
NOW = datetime.now(UTC).replace(microsecond=0)


class FakeRemnawaveApi:
    def __init__(self, user: SimpleNamespace) -> None:
        self.user = user
        self.updates: list[dict[str, Any]] = []

    async def get_user_by_uuid(self, remnawave_uuid: str) -> SimpleNamespace | None:
        return self.user if remnawave_uuid == self.user.uuid else None

    async def update_user(self, **kwargs: Any) -> SimpleNamespace:
        self.updates.append(dict(kwargs))
        if status := kwargs.get('status'):
            self.user.status = status
        if 'expire_at' in kwargs:
            self.user.expire_at = kwargs['expire_at']
        if 'traffic_limit_bytes' in kwargs:
            self.user.traffic_limit_bytes = kwargs['traffic_limit_bytes']
        if 'active_internal_squads' in kwargs:
            self.user.active_internal_squads = [{'uuid': squad_uuid} for squad_uuid in kwargs['active_internal_squads']]
        if 'external_squad_uuid' in kwargs:
            self.user.external_squad_uuid = kwargs['external_squad_uuid']
        if 'hwid_device_limit' in kwargs:
            self.user.hwid_device_limit = kwargs['hwid_device_limit']
        return self.user


def make_panel_user(
    *,
    status: UserStatus,
    expire_at: datetime,
    traffic_limit_bytes: int,
    squad_uuids: tuple[str, ...],
    external_squad_uuid: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        uuid=PANEL_UUID,
        status=status,
        expire_at=expire_at,
        traffic_limit_bytes=traffic_limit_bytes,
        used_traffic_bytes=10 * GIB,
        active_internal_squads=[{'uuid': value} for value in squad_uuids],
        external_squad_uuid=external_squad_uuid,
        user_traffic=SimpleNamespace(used_traffic_bytes=10 * GIB),
        last_traffic_reset_at=None,
        hwid_device_limit=2,
    )


def make_overlay() -> GracePanelOverlay:
    return GracePanelOverlay(
        status='ACTIVE',
        expire_at=NOW + timedelta(days=3),
        traffic_limit_bytes=11 * GIB,
        squad_uuids=(GRACE_SQUAD,),
        external_squad_uuid=None,
    )


def make_limited_billing() -> GraceBillingState:
    return GraceBillingState(
        subscription_id=42,
        remnawave_uuid=PANEL_UUID,
        status='limited',
        end_at=NOW + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
        device_limit=4,
        squad_uuids=(REGULAR_SQUAD,),
        external_squad_uuid=EXTERNAL_SQUAD,
    )


def make_limited_snapshot() -> GracePanelSnapshot:
    return GracePanelSnapshot(
        remnawave_uuid=PANEL_UUID,
        status='LIMITED',
        expire_at=NOW + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
        squad_uuids=(REGULAR_SQUAD,),
        external_squad_uuid=EXTERNAL_SQUAD,
    )


def install_fake_api(monkeypatch: pytest.MonkeyPatch, api: FakeRemnawaveApi) -> None:
    from app.services.remnawave_service import remnawave_service

    @asynccontextmanager
    async def get_api_client():
        yield api

    monkeypatch.setattr(remnawave_service, 'get_api_client', get_api_client)


def assert_no_derived_status_writes(api: FakeRemnawaveApi) -> None:
    assert all(update.get('status') not in {UserStatus.LIMITED, UserStatus.EXPIRED} for update in api.updates)


@pytest.mark.parametrize('derived_status', [UserStatus.LIMITED, UserStatus.EXPIRED])
def test_panel_target_serializer_removes_derived_statuses(
    derived_status: UserStatus,
) -> None:
    target = _PanelTarget(
        status=derived_status,
        expire_at=NOW + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        squad_uuids=(REGULAR_SQUAD,),
        external_squad_uuid=EXTERNAL_SQUAD,
        device_limit=4,
    )

    payload = _serialize_panel_target(
        PANEL_UUID,
        target,
        base_kwargs={'status': derived_status, 'description': 'preserved'},
    )

    assert 'status' not in payload
    assert payload['description'] == 'preserved'


@pytest.mark.asyncio
async def test_apply_limited_billing_restores_canonical_fields_without_writing_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    billing = make_limited_billing()
    overlay = make_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.LIMITED,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
        )
    )
    install_fake_api(monkeypatch, api)

    await RemnawaveGracePanelGateway().apply_billing_state(
        billing,
        expected_overlay=overlay,
    )

    assert_no_derived_status_writes(api)
    assert api.user.status is UserStatus.LIMITED
    assert api.user.expire_at == billing.end_at
    assert api.user.traffic_limit_bytes == billing.traffic_limit_bytes
    assert api.user.hwid_device_limit == billing.device_limit
    assert api.user.active_internal_squads == [{'uuid': REGULAR_SQUAD}]
    assert api.user.external_squad_uuid == EXTERNAL_SQUAD


@pytest.mark.asyncio
async def test_apply_limited_billing_keeps_grace_routing_until_panel_derives_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    billing = make_limited_billing()
    overlay = make_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.ACTIVE,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
        )
    )
    install_fake_api(monkeypatch, api)
    gateway = RemnawaveGracePanelGateway()

    with pytest.raises(GracePanelTransitionPending):
        await gateway.apply_billing_state(billing, expected_overlay=overlay)

    assert_no_derived_status_writes(api)
    assert api.user.status is UserStatus.ACTIVE
    assert api.user.expire_at == billing.end_at
    assert api.user.traffic_limit_bytes == billing.traffic_limit_bytes
    assert api.user.hwid_device_limit == billing.device_limit
    assert api.user.active_internal_squads == [{'uuid': GRACE_SQUAD}]
    assert api.user.external_squad_uuid is None

    api.user.status = UserStatus.LIMITED
    await gateway.apply_billing_state(billing, expected_overlay=overlay)

    assert_no_derived_status_writes(api)
    assert api.user.active_internal_squads == [{'uuid': REGULAR_SQUAD}]
    assert api.user.external_squad_uuid == EXTERNAL_SQUAD


@pytest.mark.asyncio
async def test_restore_limited_snapshot_recognizes_safe_active_intermediate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = make_limited_snapshot()
    overlay = make_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.ACTIVE,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
        )
    )
    install_fake_api(monkeypatch, api)
    gateway = RemnawaveGracePanelGateway()

    with pytest.raises(GracePanelTransitionPending):
        await gateway.restore_snapshot(PANEL_UUID, snapshot, overlay)

    assert_no_derived_status_writes(api)
    assert api.user.status is UserStatus.ACTIVE
    assert api.user.expire_at == snapshot.expire_at
    assert api.user.traffic_limit_bytes == snapshot.traffic_limit_bytes
    assert api.user.active_internal_squads == [{'uuid': GRACE_SQUAD}]
    assert api.user.external_squad_uuid is None

    api.user.status = UserStatus.LIMITED
    outcome = await gateway.restore_snapshot(PANEL_UUID, snapshot, overlay)

    assert outcome is GraceRestoreOutcome.RESTORED
    assert_no_derived_status_writes(api)
    assert api.user.active_internal_squads == [{'uuid': REGULAR_SQUAD}]
    assert api.user.external_squad_uuid == EXTERNAL_SQUAD


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('status', 'squad_uuids'),
    [
        (UserStatus.DISABLED, (GRACE_SQUAD,)),
        (UserStatus.ACTIVE, (OTHER_SQUAD,)),
    ],
)
async def test_restore_does_not_overwrite_manual_or_unrelated_panel_state(
    monkeypatch: pytest.MonkeyPatch,
    status: UserStatus,
    squad_uuids: tuple[str, ...],
) -> None:
    snapshot = make_limited_snapshot()
    overlay = make_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=status,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=squad_uuids,
        )
    )
    install_fake_api(monkeypatch, api)

    outcome = await RemnawaveGracePanelGateway().restore_snapshot(
        PANEL_UUID,
        snapshot,
        overlay,
    )

    assert outcome is GraceRestoreOutcome.CONFLICT
    assert api.updates == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('status', 'squad_uuids'),
    [
        (UserStatus.DISABLED, (GRACE_SQUAD,)),
        (UserStatus.ACTIVE, (OTHER_SQUAD,)),
    ],
)
async def test_apply_limited_billing_does_not_overwrite_manual_or_unrelated_panel_state(
    monkeypatch: pytest.MonkeyPatch,
    status: UserStatus,
    squad_uuids: tuple[str, ...],
) -> None:
    overlay = make_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=status,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=squad_uuids,
        )
    )
    install_fake_api(monkeypatch, api)

    with pytest.raises(GracePanelTransitionConflict, match='changed outside grace'):
        await RemnawaveGracePanelGateway().apply_billing_state(
            make_limited_billing(),
            expected_overlay=overlay,
        )

    assert api.updates == []


@pytest.mark.asyncio
async def test_apply_limited_billing_updates_device_limit_even_when_other_fields_already_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    billing = make_limited_billing()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.LIMITED,
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            squad_uuids=billing.squad_uuids,
            external_squad_uuid=billing.external_squad_uuid,
        )
    )
    install_fake_api(monkeypatch, api)

    await RemnawaveGracePanelGateway().apply_billing_state(
        billing,
        expected_overlay=make_overlay(),
    )

    assert api.user.hwid_device_limit == billing.device_limit
    assert api.updates == [
        {
            'uuid': PANEL_UUID,
            'hwid_device_limit': billing.device_limit,
        }
    ]
    assert_no_derived_status_writes(api)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('billing_status', 'billing_end_at', 'expected_status'),
    [
        ('active', NOW + timedelta(days=20), UserStatus.ACTIVE),
        ('disabled', NOW + timedelta(days=20), UserStatus.DISABLED),
    ],
)
async def test_apply_non_derived_billing_status_remains_one_phase(
    monkeypatch: pytest.MonkeyPatch,
    billing_status: str,
    billing_end_at: datetime,
    expected_status: UserStatus,
) -> None:
    overlay = make_overlay()
    billing = GraceBillingState(
        subscription_id=42,
        remnawave_uuid=PANEL_UUID,
        status=billing_status,
        end_at=billing_end_at,
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=3 * GIB,
        device_limit=4,
        squad_uuids=(REGULAR_SQUAD,),
        external_squad_uuid=EXTERNAL_SQUAD,
    )
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.ACTIVE,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
        )
    )
    install_fake_api(monkeypatch, api)

    await RemnawaveGracePanelGateway().apply_billing_state(
        billing,
        expected_overlay=overlay,
    )

    assert len(api.updates) == 1
    assert api.updates[0]['status'] is expected_status
    assert_no_derived_status_writes(api)
    assert api.user.active_internal_squads == [{'uuid': REGULAR_SQUAD}]
    assert api.user.external_squad_uuid == EXTERNAL_SQUAD
