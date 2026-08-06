from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.database.models import GraceAccessSessionModel
from app.external.remnawave_api import (
    RemnaWaveInvalidUserIdError,
    UserStatus,
    coerce_panel_user_id,
)
from app.services.grace_access_runtime import (
    GracePanelError,
    GraceSnapshotError,
    RemnawaveGracePanelGateway,
    SQLAlchemyGraceSessionStore,
    _billing_from_json,
    _billing_to_json,
    _build_billing_target,
    _build_restore_target,
    _model_to_session,
    _PanelTarget,
    _serialize_panel_target,
    _session_to_model,
    _session_values,
    _subscription_to_billing,
)
from app.services.grace_access_service import (
    GraceAccessSession,
    GraceBillingState,
    GraceCompletionReason,
    GracePanelOverlay,
    GracePanelSnapshot,
    GracePanelTransitionConflict,
    GracePanelTransitionPending,
    GraceReason,
    GraceRestoreOutcome,
    GraceSessionState,
    GraceTrafficResetOutcome,
    build_tariff_rebase_lineage_key,
)
from tests.fixtures.sqlite_memory import memory_session


GIB = 1024**3
# Remnawave 3.0.0 адресует пользователя числовым id; поля uuid у записи нет.
PANEL_ID = 4242
# Историческое значение колонки remnawave_uuid: встречается только в
# доапгрейдных строках и НЕ является идентификатором запроса.
LEGACY_PANEL_UUID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
GRACE_SQUAD = '11111111-1111-1111-1111-111111111111'
REGULAR_SQUAD = '22222222-2222-2222-2222-222222222222'
OTHER_SQUAD = '33333333-3333-3333-3333-333333333333'
EXTERNAL_SQUAD = '44444444-4444-4444-4444-444444444444'
NOW = datetime.now(UTC).replace(microsecond=0)


class FakeRemnawaveApi:
    """Двойник клиента 3.0.0: идентификатор всегда числовой и коерсится."""

    def __init__(self, user: SimpleNamespace) -> None:
        self.user = user
        self.updates: list[dict[str, Any]] = []
        self.reads: list[int] = []
        self.disable_calls: list[int] = []
        self.fail_update_attempts = 0
        self.fail_update_call_numbers: set[int] = set()
        self.reset_calls: list[int] = []
        self.fail_reset_after_effect = 0

    async def get_user_by_id(self, user_id: int) -> SimpleNamespace | None:
        # Как и настоящий клиент: непригодный локальный идентификатор — это
        # исключение на границе, а не «пользователя нет».
        panel_user_id = coerce_panel_user_id(user_id)
        self.reads.append(panel_user_id)
        return self.user if panel_user_id == self.user.id else None

    async def update_user(self, *, user_id: int, **kwargs: Any) -> SimpleNamespace:
        # Ключевое слово именно user_id: тело PATCH в 3.0.0 — {'id': ...},
        # а поля uuid схема запроса не содержит вовсе.
        panel_user_id = coerce_panel_user_id(user_id)
        self.updates.append({'user_id': panel_user_id, **kwargs})
        if len(self.updates) in self.fail_update_call_numbers:
            raise RuntimeError('temporary update failure')
        if self.fail_update_attempts > 0:
            self.fail_update_attempts -= 1
            raise RuntimeError('temporary update failure')
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

    async def disable_user(self, user_id: int) -> SimpleNamespace:
        panel_user_id = coerce_panel_user_id(user_id)
        self.disable_calls.append(panel_user_id)
        assert panel_user_id == self.user.id
        self.user.status = UserStatus.DISABLED
        return self.user

    async def reset_user_traffic(self, user_id: int) -> SimpleNamespace:
        panel_user_id = coerce_panel_user_id(user_id)
        self.reset_calls.append(panel_user_id)
        assert panel_user_id == self.user.id
        self.user.used_traffic_bytes = 0
        self.user.user_traffic.used_traffic_bytes = 0
        self.user.last_traffic_reset_at = NOW + timedelta(seconds=len(self.reset_calls))
        self.user.status = UserStatus.ACTIVE
        if self.fail_reset_after_effect > 0:
            self.fail_reset_after_effect -= 1
            raise RuntimeError('lost reset response')
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
        id=PANEL_ID,
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
        remnawave_id=PANEL_ID,
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
        remnawave_id=PANEL_ID,
        status='LIMITED',
        expire_at=NOW + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
        squad_uuids=(REGULAR_SQUAD,),
        external_squad_uuid=EXTERNAL_SQUAD,
    )


def make_v2_session_row(*, remnawave_id: int | None = PANEL_ID) -> GraceAccessSessionModel:
    """Строка, записанная до апгрейда панели: snapshot_version=2 и uuid в JSON.

    Числового id блобы не знают — панель 2.8.x его не отдавала, поэтому он есть
    только в бэкфилленной колонке строки.
    """
    started_at = NOW - timedelta(hours=1)
    end_at = NOW - timedelta(days=1)
    return GraceAccessSessionModel(
        id='11111111-2222-3333-4444-555555555555',
        subscription_id=42,
        remnawave_id=remnawave_id,
        remnawave_uuid=LEGACY_PANEL_UUID,
        reason=GraceReason.EXPIRED.value,
        incident_key=f'expired:{end_at.isoformat()}',
        state=GraceSessionState.ACTIVE.value,
        snapshot_version=2,
        billing_before={
            'subscription_id': 42,
            'remnawave_uuid': LEGACY_PANEL_UUID,
            'status': 'expired',
            'end_at': end_at.isoformat(),
            'traffic_limit_bytes': 10 * GIB,
            'used_traffic_bytes': 10 * GIB,
            'device_limit': 4,
            'squad_uuids': [REGULAR_SQUAD],
            'external_squad_uuid': EXTERNAL_SQUAD,
            'is_trial': False,
            'is_daily': False,
            'is_free_tariff': False,
            'user_status': 'active',
            'grace_suppressed_until': None,
        },
        panel_before={
            'remnawave_uuid': LEGACY_PANEL_UUID,
            'status': 'EXPIRED',
            'expire_at': end_at.isoformat(),
            'traffic_limit_bytes': 10 * GIB,
            'used_traffic_bytes': 10 * GIB,
            'squad_uuids': [REGULAR_SQUAD],
            'external_squad_uuid': EXTERNAL_SQUAD,
            'traffic_is_known': True,
            'last_traffic_reset_at': None,
        },
        overlay={
            'status': 'ACTIVE',
            'expire_at': (NOW + timedelta(days=3)).isoformat(),
            'traffic_limit_bytes': 11 * GIB,
            'squad_uuids': [GRACE_SQUAD],
            'external_squad_uuid': None,
        },
        started_at=started_at,
        grace_until=NOW + timedelta(days=3),
        updated_at=started_at,
        completion_reason=None,
        completed_at=None,
        last_error=None,
        version=1,
    )


def make_expired_overlay() -> GracePanelOverlay:
    return GracePanelOverlay(
        status='ACTIVE',
        expire_at=NOW - timedelta(minutes=1),
        traffic_limit_bytes=11 * GIB,
        squad_uuids=(GRACE_SQUAD,),
        external_squad_uuid=None,
    )


def make_expired_snapshot() -> GracePanelSnapshot:
    return GracePanelSnapshot(
        remnawave_id=PANEL_ID,
        status='EXPIRED',
        expire_at=NOW - timedelta(days=7),
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


def test_grace_billing_json_round_trip_preserves_tariff_identity_and_legacy_default() -> None:
    billing = replace(
        make_limited_billing(),
        tariff_id=7,
        tariff_id_known=True,
    )

    assert _billing_from_json(_billing_to_json(billing)) == billing

    legacy = _billing_to_json(billing)
    legacy.pop('tariff_id')
    legacy.pop('tariff_id_known')
    restored_legacy = _billing_from_json(legacy)
    assert restored_legacy.tariff_id is None
    assert restored_legacy.tariff_id_known is False

    real_null_tariff = replace(billing, tariff_id=None, tariff_id_known=True)
    assert _billing_from_json(_billing_to_json(real_null_tariff)) == real_null_tariff


def test_grace_session_json_round_trip_preserves_incident_aliases() -> None:
    billing = replace(make_limited_billing(), tariff_id=2, tariff_id_known=True)
    snapshot = make_limited_snapshot()
    overlay = make_overlay()
    session = GraceAccessSession(
        id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        subscription_id=billing.subscription_id,
        remnawave_id=PANEL_ID,
        reason=GraceReason.LIMITED,
        incident_key='limited:primary',
        state=GraceSessionState.ACTIVE,
        billing_before=billing,
        panel_before=snapshot,
        overlay=overlay,
        started_at=NOW,
        grace_until=overlay.expire_at,
        updated_at=NOW,
        incident_aliases=('limited:rebased', 'tariff-rebase:limited:lineage'),
        limited_lineage_tail=billing,
        allow_recovery_enabled_webhook=True,
        traffic_reset_target=replace(
            billing,
            tariff_id=3,
            used_traffic_bytes=0,
        ),
        traffic_reset_remaining_bytes=GIB // 2,
        traffic_reset_started_at=NOW,
        traffic_reset_finished_at=NOW + timedelta(seconds=1),
    )

    restored = _model_to_session(_session_to_model(session))

    assert restored == session
    applied_fence_proof = replace(session, traffic_reset_target=None)
    assert _model_to_session(_session_to_model(applied_fence_proof)) == applied_fence_proof


def test_legacy_grace_session_without_tariff_or_lineage_metadata_still_loads() -> None:
    billing = replace(make_limited_billing(), tariff_id=2, tariff_id_known=True)
    overlay = make_overlay()
    session = GraceAccessSession(
        id='aaaaaaaa-bbbb-cccc-dddd-ffffffffffff',
        subscription_id=billing.subscription_id,
        remnawave_id=PANEL_ID,
        reason=GraceReason.LIMITED,
        incident_key='limited:legacy',
        state=GraceSessionState.ACTIVE,
        billing_before=billing,
        panel_before=make_limited_snapshot(),
        overlay=overlay,
        started_at=NOW,
        grace_until=overlay.expire_at,
        updated_at=NOW,
    )
    model = _session_to_model(session)
    model.billing_before = dict(model.billing_before)
    model.billing_before.pop('tariff_id')
    model.billing_before.pop('tariff_id_known')
    model.overlay = dict(model.overlay)
    model.overlay.pop('_incident_aliases', None)
    model.overlay.pop('_limited_lineage_tail', None)
    model.overlay.pop('_allow_recovery_enabled_webhook', None)
    model.overlay.pop('_traffic_reset_target', None)
    model.overlay.pop('_traffic_reset_remaining_bytes', None)
    model.overlay.pop('_traffic_reset_started_at', None)
    model.overlay.pop('_traffic_reset_finished_at', None)

    restored = _model_to_session(model)

    assert restored.billing_before.tariff_id is None
    assert restored.billing_before.tariff_id_known is False
    assert restored.incident_aliases == ()
    assert restored.limited_lineage_tail is None
    assert restored.allow_recovery_enabled_webhook is False
    assert restored.traffic_reset_target is None
    assert restored.traffic_reset_remaining_bytes is None
    assert restored.traffic_reset_started_at is None
    assert restored.traffic_reset_finished_at is None


@pytest.mark.asyncio
async def test_sqlalchemy_store_finds_persisted_incident_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    billing = replace(make_limited_billing(), tariff_id=2, tariff_id_known=True)
    overlay = make_overlay()
    session = GraceAccessSession(
        id='aaaaaaaa-bbbb-cccc-dddd-111111111111',
        subscription_id=billing.subscription_id,
        remnawave_id=PANEL_ID,
        reason=GraceReason.LIMITED,
        incident_key='limited:primary',
        state=GraceSessionState.COMPLETED,
        billing_before=billing,
        panel_before=make_limited_snapshot(),
        overlay=overlay,
        started_at=NOW,
        grace_until=overlay.expire_at,
        updated_at=NOW,
        completion_reason=GraceCompletionReason.TIMEOUT,
        completed_at=NOW,
        incident_aliases=('limited:rebased', 'tariff-rebase:limited:lineage'),
        limited_lineage_tail=billing,
    )

    async with memory_session(monkeypatch, [GraceAccessSessionModel.__table__]) as db:
        db.add(_session_to_model(session))
        await db.commit()
        store = SQLAlchemyGraceSessionStore(db)

        primary = await store.get_by_incident(session.subscription_id, session.incident_key)
        alias = await store.get_by_incident(session.subscription_id, 'limited:rebased')
        missing = await store.get_by_incident(session.subscription_id, 'limited:new-reset')

    assert primary is not None and primary.id == session.id
    assert alias is not None and alias.id == session.id
    assert alias.limited_lineage_tail == billing
    assert missing is None


@pytest.mark.asyncio
async def test_sqlalchemy_store_derives_lineage_for_legacy_limited_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_at = NOW - timedelta(days=10)
    billing = replace(
        make_limited_billing(),
        tariff_id=None,
        tariff_id_known=False,
    )
    snapshot = replace(make_limited_snapshot(), last_traffic_reset_at=reset_at)
    overlay = make_overlay()
    session = GraceAccessSession(
        id='aaaaaaaa-bbbb-cccc-dddd-222222222222',
        subscription_id=billing.subscription_id,
        remnawave_id=PANEL_ID,
        reason=GraceReason.LIMITED,
        incident_key='limited:legacy-primary',
        state=GraceSessionState.COMPLETED,
        billing_before=billing,
        panel_before=snapshot,
        overlay=overlay,
        started_at=NOW,
        grace_until=overlay.expire_at,
        updated_at=NOW,
        completion_reason=GraceCompletionReason.CONFLICT,
        completed_at=NOW,
    )
    lineage_key = build_tariff_rebase_lineage_key(
        billing,
        GraceReason.LIMITED,
        last_traffic_reset_at=reset_at,
    )

    async with memory_session(monkeypatch, [GraceAccessSessionModel.__table__]) as db:
        db.add(_session_to_model(session))
        await db.commit()
        restored = await SQLAlchemyGraceSessionStore(db).get_by_incident(
            session.subscription_id,
            lineage_key,
        )

    assert restored is not None and restored.id == session.id
    assert restored.incident_aliases == ()
    assert restored.limited_lineage_tail is None


@pytest.mark.parametrize('tariff_id', [7, None])
def test_subscription_to_billing_marks_live_tariff_identity_as_known(
    monkeypatch: pytest.MonkeyPatch,
    tariff_id: int | None,
) -> None:
    from app.config import settings

    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    subscription = SimpleNamespace(
        id=42,
        user=SimpleNamespace(remnawave_id=PANEL_ID, status='active'),
        tariff=SimpleNamespace(
            external_squad_uuid=EXTERNAL_SQUAD,
            is_daily=False,
            is_free=False,
        ),
        remnawave_id=None,
        actual_status='limited',
        end_date=NOW + timedelta(days=20),
        traffic_limit_gb=10,
        traffic_used_gb=10.0,
        device_limit=2,
        connected_squads=[REGULAR_SQUAD],
        is_trial=False,
        status='limited',
        grace_suppressed_until=None,
        tariff_id=tariff_id,
    )

    billing = _subscription_to_billing(subscription)

    assert billing.tariff_id == tariff_id
    assert billing.tariff_id_known is True


@pytest.mark.asyncio
async def test_prepare_tariff_rebase_changes_only_device_limit_and_verifies_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = make_overlay()
    user = make_panel_user(
        status=UserStatus.ACTIVE,
        expire_at=overlay.expire_at,
        traffic_limit_bytes=overlay.traffic_limit_bytes,
        squad_uuids=overlay.squad_uuids,
        external_squad_uuid=overlay.external_squad_uuid,
    )
    api = FakeRemnawaveApi(user)
    install_fake_api(monkeypatch, api)
    billing = replace(
        make_limited_billing(),
        tariff_id=2,
        tariff_id_known=True,
        device_limit=4,
    )

    prepared = await RemnawaveGracePanelGateway().prepare_tariff_rebase(
        billing,
        expected_overlay=overlay,
        expected_last_traffic_reset_at=None,
    )

    assert prepared is not None
    assert api.updates == [{'user_id': PANEL_ID, 'hwid_device_limit': 4}]
    assert user.expire_at == overlay.expire_at
    assert user.traffic_limit_bytes == overlay.traffic_limit_bytes
    assert tuple(item['uuid'] for item in user.active_internal_squads) == overlay.squad_uuids
    assert user.external_squad_uuid == overlay.external_squad_uuid
    assert_no_derived_status_writes(api)


@pytest.mark.asyncio
async def test_prepare_tariff_rebase_avoids_patch_when_device_limit_already_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = make_overlay()
    user = make_panel_user(
        status=UserStatus.ACTIVE,
        expire_at=overlay.expire_at,
        traffic_limit_bytes=overlay.traffic_limit_bytes,
        squad_uuids=overlay.squad_uuids,
    )
    api = FakeRemnawaveApi(user)
    install_fake_api(monkeypatch, api)
    billing = replace(
        make_limited_billing(),
        tariff_id=2,
        tariff_id_known=True,
        device_limit=user.hwid_device_limit,
    )

    prepared = await RemnawaveGracePanelGateway().prepare_tariff_rebase(
        billing,
        expected_overlay=overlay,
        expected_last_traffic_reset_at=None,
    )

    assert prepared is not None
    assert api.updates == []


@pytest.mark.asyncio
async def test_prepare_tariff_rebase_rejects_changed_traffic_reset_before_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = make_overlay()
    user = make_panel_user(
        status=UserStatus.ACTIVE,
        expire_at=overlay.expire_at,
        traffic_limit_bytes=overlay.traffic_limit_bytes,
        squad_uuids=overlay.squad_uuids,
    )
    user.last_traffic_reset_at = NOW
    api = FakeRemnawaveApi(user)
    install_fake_api(monkeypatch, api)

    prepared = await RemnawaveGracePanelGateway().prepare_tariff_rebase(
        replace(make_limited_billing(), tariff_id=2, tariff_id_known=True),
        expected_overlay=overlay,
        expected_last_traffic_reset_at=NOW - timedelta(days=1),
    )

    assert prepared is None
    assert api.updates == []


@pytest.mark.asyncio
async def test_limited_tariff_reset_fences_quota_then_restores_canonical_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = make_overlay()
    user = make_panel_user(
        status=UserStatus.ACTIVE,
        expire_at=overlay.expire_at,
        traffic_limit_bytes=overlay.traffic_limit_bytes,
        squad_uuids=overlay.squad_uuids,
    )
    # A reset one second after the previous one is still a distinct generation.
    user.last_traffic_reset_at = NOW
    api = FakeRemnawaveApi(user)
    install_fake_api(monkeypatch, api)
    billing = replace(
        make_limited_billing(),
        tariff_id=2,
        tariff_id_known=True,
        traffic_limit_bytes=5 * GIB,
        used_traffic_bytes=0,
    )

    result = await RemnawaveGracePanelGateway().apply_tariff_switch_traffic_reset(
        billing,
        reason=GraceReason.LIMITED,
        expected_overlay=overlay,
        expected_last_traffic_reset_at=NOW,
        remaining_grace_bytes=GIB,
    )

    assert result.outcome is GraceTrafficResetOutcome.RECOVERED
    assert api.reset_calls == [PANEL_ID]
    assert api.updates[0]['traffic_limit_bytes'] == GIB
    assert 'status' not in api.updates[0]
    assert api.updates[-1]['status'] is UserStatus.ACTIVE
    assert user.used_traffic_bytes == 0
    assert user.traffic_limit_bytes == billing.traffic_limit_bytes
    assert tuple(item['uuid'] for item in user.active_internal_squads) == billing.squad_uuids
    assert_no_derived_status_writes(api)


@pytest.mark.asyncio
async def test_expired_tariff_reset_keeps_only_remaining_grace_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = make_overlay()
    user = make_panel_user(
        status=UserStatus.ACTIVE,
        expire_at=overlay.expire_at,
        traffic_limit_bytes=overlay.traffic_limit_bytes,
        squad_uuids=overlay.squad_uuids,
    )
    user.used_traffic_bytes = 10 * GIB + GIB // 4
    user.user_traffic.used_traffic_bytes = user.used_traffic_bytes
    api = FakeRemnawaveApi(user)
    install_fake_api(monkeypatch, api)
    billing = replace(
        make_limited_billing(),
        status='expired',
        end_at=NOW - timedelta(days=1),
        tariff_id=2,
        tariff_id_known=True,
        traffic_limit_bytes=5 * GIB,
        used_traffic_bytes=0,
    )

    result = await RemnawaveGracePanelGateway().apply_tariff_switch_traffic_reset(
        billing,
        reason=GraceReason.EXPIRED,
        expected_overlay=overlay,
        expected_last_traffic_reset_at=None,
        remaining_grace_bytes=3 * GIB // 4,
    )

    assert result.outcome is GraceTrafficResetOutcome.CONTINUED
    assert result.overlay is not None
    assert result.overlay.expire_at == overlay.expire_at
    assert result.overlay.traffic_limit_bytes == 3 * GIB // 4
    assert result.overlay.squad_uuids == overlay.squad_uuids
    assert api.reset_calls == [PANEL_ID]
    assert user.used_traffic_bytes == 0
    assert user.traffic_limit_bytes == 3 * GIB // 4
    assert tuple(item['uuid'] for item in user.active_internal_squads) == overlay.squad_uuids
    assert_no_derived_status_writes(api)


@pytest.mark.asyncio
async def test_expired_tariff_reset_retry_recognizes_changed_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = make_overlay()
    user = make_panel_user(
        status=UserStatus.ACTIVE,
        expire_at=overlay.expire_at,
        traffic_limit_bytes=overlay.traffic_limit_bytes,
        squad_uuids=overlay.squad_uuids,
    )
    api = FakeRemnawaveApi(user)
    api.fail_reset_after_effect = 1
    install_fake_api(monkeypatch, api)
    billing = replace(
        make_limited_billing(),
        status='expired',
        end_at=NOW - timedelta(days=1),
        tariff_id=2,
        tariff_id_known=True,
        traffic_limit_bytes=5 * GIB,
        used_traffic_bytes=0,
    )
    gateway = RemnawaveGracePanelGateway()

    with pytest.raises(RuntimeError, match='lost reset response'):
        await gateway.apply_tariff_switch_traffic_reset(
            billing,
            reason=GraceReason.EXPIRED,
            expected_overlay=overlay,
            expected_last_traffic_reset_at=None,
            remaining_grace_bytes=GIB,
        )

    result = await gateway.apply_tariff_switch_traffic_reset(
        billing,
        reason=GraceReason.EXPIRED,
        expected_overlay=overlay,
        expected_last_traffic_reset_at=None,
        remaining_grace_bytes=GIB,
    )

    assert result.outcome is GraceTrafficResetOutcome.CONTINUED
    assert api.reset_calls == [PANEL_ID]


@pytest.mark.asyncio
async def test_zero_remaining_grace_never_becomes_unlimited_after_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = make_overlay()
    user = make_panel_user(
        status=UserStatus.LIMITED,
        expire_at=overlay.expire_at,
        traffic_limit_bytes=overlay.traffic_limit_bytes,
        squad_uuids=overlay.squad_uuids,
    )
    user.used_traffic_bytes = overlay.traffic_limit_bytes
    user.user_traffic.used_traffic_bytes = user.used_traffic_bytes
    api = FakeRemnawaveApi(user)
    install_fake_api(monkeypatch, api)
    billing = replace(
        make_limited_billing(),
        status='expired',
        end_at=NOW - timedelta(days=1),
        tariff_id=2,
        tariff_id_known=True,
        used_traffic_bytes=0,
    )

    result = await RemnawaveGracePanelGateway().apply_tariff_switch_traffic_reset(
        billing,
        reason=GraceReason.EXPIRED,
        expected_overlay=overlay,
        expected_last_traffic_reset_at=None,
        remaining_grace_bytes=0,
    )

    assert result.outcome is GraceTrafficResetOutcome.EXHAUSTED
    assert api.reset_calls == [PANEL_ID]
    assert all(update.get('traffic_limit_bytes') != 0 for update in api.updates[:-1])
    assert user.status in {UserStatus.DISABLED, UserStatus.EXPIRED}
    assert_no_derived_status_writes(api)


@pytest.mark.asyncio
async def test_missing_billing_revocation_disables_only_exact_reset_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = make_overlay()
    user = make_panel_user(
        status=UserStatus.ACTIVE,
        expire_at=overlay.expire_at,
        traffic_limit_bytes=overlay.traffic_limit_bytes,
        squad_uuids=overlay.squad_uuids,
        external_squad_uuid=overlay.external_squad_uuid,
    )
    api = FakeRemnawaveApi(user)
    install_fake_api(monkeypatch, api)

    await RemnawaveGracePanelGateway().revoke_missing_billing(
        PANEL_ID,
        expected_overlay=overlay,
    )

    assert api.disable_calls == [PANEL_ID]
    assert user.status is UserStatus.DISABLED


@pytest.mark.asyncio
async def test_missing_billing_revocation_preserves_unrelated_panel_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = make_overlay()
    user = make_panel_user(
        status=UserStatus.ACTIVE,
        expire_at=overlay.expire_at,
        traffic_limit_bytes=overlay.traffic_limit_bytes,
        squad_uuids=(OTHER_SQUAD,),
    )
    api = FakeRemnawaveApi(user)
    install_fake_api(monkeypatch, api)

    with pytest.raises(GracePanelTransitionConflict, match='outside'):
        await RemnawaveGracePanelGateway().revoke_missing_billing(
            PANEL_ID,
            expected_overlay=overlay,
        )

    assert api.disable_calls == []


@pytest.mark.asyncio
async def test_tariff_recovery_applies_active_only_from_exact_overlay_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = make_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.ACTIVE,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
            external_squad_uuid=overlay.external_squad_uuid,
        )
    )
    install_fake_api(monkeypatch, api)
    recovered = replace(
        make_limited_billing(),
        status='active',
        traffic_limit_bytes=20 * GIB,
        tariff_id=2,
        tariff_id_known=True,
    )

    await RemnawaveGracePanelGateway().apply_billing_state(
        recovered,
        expected_overlay=overlay,
        require_overlay_source=True,
        expected_last_traffic_reset_at=None,
    )

    assert len(api.updates) == 1
    assert api.updates[0]['status'] is UserStatus.ACTIVE
    assert api.user.traffic_limit_bytes == recovered.traffic_limit_bytes
    assert api.user.active_internal_squads == [{'uuid': REGULAR_SQUAD}]


@pytest.mark.asyncio
async def test_tariff_recovery_rejects_active_patch_after_overlay_source_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = make_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.ACTIVE,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=(OTHER_SQUAD,),
            external_squad_uuid=overlay.external_squad_uuid,
        )
    )
    install_fake_api(monkeypatch, api)

    with pytest.raises(GracePanelTransitionConflict, match='outside grace'):
        await RemnawaveGracePanelGateway().apply_billing_state(
            replace(
                make_limited_billing(),
                status='active',
                traffic_limit_bytes=20 * GIB,
                tariff_id=2,
                tariff_id_known=True,
            ),
            expected_overlay=overlay,
            require_overlay_source=True,
            expected_last_traffic_reset_at=None,
        )

    assert api.updates == []


@pytest.mark.asyncio
async def test_restore_expired_snapshot_preserves_overlay_expiry_without_status_or_expire_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = make_expired_snapshot()
    overlay = make_expired_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.EXPIRED,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
        )
    )
    install_fake_api(monkeypatch, api)

    outcome = await RemnawaveGracePanelGateway().restore_snapshot(
        PANEL_ID,
        snapshot,
        overlay,
    )

    assert outcome is GraceRestoreOutcome.RESTORED
    assert len(api.updates) == 1
    assert 'status' not in api.updates[0]
    assert 'expire_at' not in api.updates[0]
    assert api.user.status is UserStatus.EXPIRED
    assert api.user.expire_at == overlay.expire_at
    assert api.user.traffic_limit_bytes == snapshot.traffic_limit_bytes
    assert api.user.active_internal_squads == [{'uuid': REGULAR_SQUAD}]
    assert api.user.external_squad_uuid == EXTERNAL_SQUAD


@pytest.mark.asyncio
async def test_restore_expired_snapshot_keeps_grace_routing_while_waiting_for_derived_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = make_expired_snapshot()
    overlay = make_expired_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.ACTIVE,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
        )
    )
    install_fake_api(monkeypatch, api)

    with pytest.raises(GracePanelTransitionPending):
        await RemnawaveGracePanelGateway().restore_snapshot(
            PANEL_ID,
            snapshot,
            overlay,
        )

    assert api.updates == []
    assert api.user.status is UserStatus.ACTIVE
    assert api.user.expire_at == overlay.expire_at
    assert api.user.active_internal_squads == [{'uuid': GRACE_SQUAD}]
    assert api.user.external_squad_uuid is None


@pytest.mark.asyncio
async def test_force_restore_expired_snapshot_disables_then_restores_fields_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = make_expired_snapshot()
    overlay = make_expired_overlay()
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

    first = await gateway.restore_snapshot(
        PANEL_ID,
        snapshot,
        overlay,
        force_disable=True,
    )
    second = await gateway.restore_snapshot(
        PANEL_ID,
        snapshot,
        overlay,
        force_disable=True,
    )

    assert first is GraceRestoreOutcome.RESTORED
    assert second is GraceRestoreOutcome.ALREADY_RESTORED
    assert api.disable_calls == []
    assert len(api.updates) == 2
    assert api.updates[0] == {
        'user_id': PANEL_ID,
        'status': UserStatus.DISABLED,
    }
    assert 'status' not in api.updates[1]
    assert 'expire_at' not in api.updates[1]
    assert api.user.status is UserStatus.DISABLED
    assert api.user.expire_at == overlay.expire_at
    assert api.user.traffic_limit_bytes == snapshot.traffic_limit_bytes
    assert api.user.active_internal_squads == [{'uuid': REGULAR_SQUAD}]
    assert api.user.external_squad_uuid == EXTERNAL_SQUAD


@pytest.mark.asyncio
async def test_force_restore_retries_exact_disabled_overlay_after_field_patch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = make_expired_snapshot()
    overlay = make_expired_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.ACTIVE,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
        )
    )
    api.fail_update_call_numbers = {2}
    install_fake_api(monkeypatch, api)
    gateway = RemnawaveGracePanelGateway()

    with pytest.raises(RuntimeError, match='temporary update failure'):
        await gateway.restore_snapshot(
            PANEL_ID,
            snapshot,
            overlay,
            force_disable=True,
        )

    assert api.user.status is UserStatus.DISABLED
    assert api.user.active_internal_squads == [{'uuid': GRACE_SQUAD}]

    outcome = await gateway.restore_snapshot(
        PANEL_ID,
        snapshot,
        overlay,
        force_disable=True,
    )

    assert outcome is GraceRestoreOutcome.RESTORED
    assert api.disable_calls == []
    assert api.updates[0] == {
        'user_id': PANEL_ID,
        'status': UserStatus.DISABLED,
    }
    assert all('status' not in update for update in api.updates[1:])
    assert api.user.status is UserStatus.DISABLED
    assert api.user.traffic_limit_bytes == snapshot.traffic_limit_bytes
    assert api.user.active_internal_squads == [{'uuid': REGULAR_SQUAD}]
    assert api.user.external_squad_uuid == EXTERNAL_SQUAD


@pytest.mark.asyncio
async def test_force_restore_does_not_emit_user_disabled_for_limited_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = make_expired_snapshot()
    overlay = make_expired_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.LIMITED,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
        )
    )
    install_fake_api(monkeypatch, api)

    with pytest.raises(GracePanelTransitionPending):
        await RemnawaveGracePanelGateway().restore_snapshot(
            PANEL_ID,
            snapshot,
            overlay,
            force_disable=True,
        )

    assert api.disable_calls == []
    assert api.updates == []
    assert api.user.status is UserStatus.LIMITED
    assert api.user.active_internal_squads == [{'uuid': GRACE_SQUAD}]


@pytest.mark.asyncio
async def test_restore_disabled_snapshot_restores_future_expiry_in_field_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = make_overlay()
    snapshot = replace(
        make_expired_snapshot(),
        status='DISABLED',
        expire_at=NOW + timedelta(days=20),
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

    outcome = await RemnawaveGracePanelGateway().restore_snapshot(
        PANEL_ID,
        snapshot,
        overlay,
    )

    assert outcome is GraceRestoreOutcome.RESTORED
    assert api.disable_calls == []
    assert api.updates[0] == {
        'user_id': PANEL_ID,
        'status': UserStatus.DISABLED,
    }
    assert 'status' not in api.updates[1]
    assert api.updates[1]['expire_at'] == snapshot.expire_at
    assert api.user.status is UserStatus.DISABLED
    assert api.user.expire_at == snapshot.expire_at
    assert api.user.active_internal_squads == [{'uuid': REGULAR_SQUAD}]


@pytest.mark.asyncio
async def test_apply_disabled_billing_does_not_patch_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = make_expired_overlay()
    billing = GraceBillingState(
        subscription_id=42,
        remnawave_id=PANEL_ID,
        status='disabled',
        end_at=NOW - timedelta(days=7),
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

    assert api.disable_calls == [PANEL_ID]
    assert len(api.updates) == 1
    assert 'status' not in api.updates[0]
    assert 'expire_at' not in api.updates[0]
    assert api.user.expire_at == overlay.expire_at


@pytest.mark.asyncio
async def test_apply_expired_billing_uses_modified_phases_without_user_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = make_expired_overlay()
    billing = GraceBillingState(
        subscription_id=42,
        remnawave_id=PANEL_ID,
        status='expired',
        end_at=NOW - timedelta(days=7),
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

    assert api.disable_calls == []
    assert api.updates[0] == {
        'user_id': PANEL_ID,
        'status': UserStatus.DISABLED,
    }
    assert 'status' not in api.updates[1]
    assert 'expire_at' not in api.updates[1]
    assert api.user.status is UserStatus.DISABLED
    assert api.user.expire_at == overlay.expire_at
    assert api.user.traffic_limit_bytes == billing.traffic_limit_bytes
    assert api.user.active_internal_squads == [{'uuid': REGULAR_SQUAD}]


def test_disabled_targets_do_not_synthesize_one_minute_expiry() -> None:
    now = NOW
    expired_at = now - timedelta(days=7)
    snapshot = make_expired_snapshot()
    billing = GraceBillingState(
        subscription_id=42,
        remnawave_id=PANEL_ID,
        status='disabled',
        end_at=expired_at,
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=3 * GIB,
        device_limit=4,
        squad_uuids=(REGULAR_SQUAD,),
        external_squad_uuid=EXTERNAL_SQUAD,
    )

    restore_target = _build_restore_target(snapshot, now=now)
    billing_target = _build_billing_target(billing, now=now)

    assert restore_target.status is UserStatus.EXPIRED
    assert billing_target.status is UserStatus.DISABLED
    assert restore_target.expire_at == snapshot.expire_at
    assert billing_target.expire_at == expired_at
    assert restore_target.expire_at != now + timedelta(minutes=1)
    assert billing_target.expire_at != now + timedelta(minutes=1)


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
        PANEL_ID,
        target,
        base_kwargs={'status': derived_status, 'description': 'preserved'},
    )

    assert 'status' not in payload
    assert payload['description'] == 'preserved'
    # Панель 3.0.0 адресуется числовым id; ключ uuid схема PATCH срезает молча.
    assert payload['user_id'] == PANEL_ID
    assert 'uuid' not in payload


@pytest.mark.parametrize('seconds_until_expiry', [-300, 0, 59, 60])
def test_panel_target_serializer_rejects_active_expiry_inside_safety_margin(
    seconds_until_expiry: int,
) -> None:
    target = _PanelTarget(
        status=UserStatus.ACTIVE,
        expire_at=NOW + timedelta(seconds=seconds_until_expiry),
        traffic_limit_bytes=10 * GIB,
        squad_uuids=(REGULAR_SQUAD,),
        external_squad_uuid=EXTERNAL_SQUAD,
    )

    with pytest.raises(GracePanelError, match='not safely in the future'):
        _serialize_panel_target(PANEL_ID, target, now=NOW)


@pytest.mark.parametrize('seconds_until_expiry', [-300, 0, 59, 60])
def test_panel_target_serializer_omits_unsafe_expiry_for_disabled_target(
    seconds_until_expiry: int,
) -> None:
    target = _PanelTarget(
        status=UserStatus.DISABLED,
        expire_at=NOW + timedelta(seconds=seconds_until_expiry),
        traffic_limit_bytes=10 * GIB,
        squad_uuids=(REGULAR_SQUAD,),
        external_squad_uuid=EXTERNAL_SQUAD,
    )

    payload = _serialize_panel_target(PANEL_ID, target, now=NOW)

    assert payload['status'] is UserStatus.DISABLED
    assert 'expire_at' not in payload


def test_panel_target_serializer_normalizes_safe_expiry_to_utc() -> None:
    expected_expiry = NOW + timedelta(minutes=2)
    source_timezone = timezone(timedelta(hours=3))
    target = _PanelTarget(
        status=UserStatus.ACTIVE,
        expire_at=expected_expiry.astimezone(source_timezone),
        traffic_limit_bytes=10 * GIB,
        squad_uuids=(REGULAR_SQUAD,),
        external_squad_uuid=EXTERNAL_SQUAD,
    )

    payload = _serialize_panel_target(PANEL_ID, target, now=NOW)

    assert payload['expire_at'] == expected_expiry
    assert payload['expire_at'].tzinfo is UTC


def test_panel_target_serializer_rejects_naive_expiry() -> None:
    target = _PanelTarget(
        status=UserStatus.ACTIVE,
        expire_at=(NOW + timedelta(minutes=2)).replace(tzinfo=None),
        traffic_limit_bytes=10 * GIB,
        squad_uuids=(REGULAR_SQUAD,),
        external_squad_uuid=EXTERNAL_SQUAD,
    )

    with pytest.raises(GracePanelError, match='timezone-aware'):
        _serialize_panel_target(PANEL_ID, target, now=NOW)


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
async def test_apply_limited_billing_accepts_exact_previous_restored_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = make_limited_snapshot()
    overlay = make_overlay()
    billing = replace(
        make_limited_billing(),
        traffic_limit_bytes=5 * GIB,
        squad_uuids=(OTHER_SQUAD,),
    )
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.LIMITED,
            expire_at=previous.expire_at,
            traffic_limit_bytes=previous.traffic_limit_bytes,
            squad_uuids=previous.squad_uuids,
            external_squad_uuid=previous.external_squad_uuid,
        )
    )
    install_fake_api(monkeypatch, api)

    await RemnawaveGracePanelGateway().apply_billing_state(
        billing,
        expected_overlay=overlay,
        expected_restored_snapshot=previous,
    )

    assert_no_derived_status_writes(api)
    assert api.user.status is UserStatus.LIMITED
    assert api.user.traffic_limit_bytes == billing.traffic_limit_bytes
    assert api.user.active_internal_squads == [{'uuid': OTHER_SQUAD}]
    assert api.user.external_squad_uuid == billing.external_squad_uuid


@pytest.mark.asyncio
async def test_apply_limited_billing_accepts_previous_restore_intermediate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = make_limited_snapshot()
    overlay = make_overlay()
    billing = replace(
        make_limited_billing(),
        traffic_limit_bytes=5 * GIB,
        squad_uuids=(OTHER_SQUAD,),
    )
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.ACTIVE,
            expire_at=previous.expire_at,
            traffic_limit_bytes=previous.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
            external_squad_uuid=overlay.external_squad_uuid,
        )
    )
    install_fake_api(monkeypatch, api)
    gateway = RemnawaveGracePanelGateway()

    with pytest.raises(GracePanelTransitionPending):
        await gateway.apply_billing_state(
            billing,
            expected_overlay=overlay,
            expected_restored_snapshot=previous,
        )

    assert_no_derived_status_writes(api)
    assert api.user.status is UserStatus.ACTIVE
    assert api.user.traffic_limit_bytes == billing.traffic_limit_bytes
    assert api.user.active_internal_squads == [{'uuid': GRACE_SQUAD}]

    api.user.status = UserStatus.LIMITED
    await gateway.apply_billing_state(
        billing,
        expected_overlay=overlay,
        expected_restored_snapshot=previous,
    )

    assert_no_derived_status_writes(api)
    assert api.user.active_internal_squads == [{'uuid': OTHER_SQUAD}]


@pytest.mark.asyncio
async def test_apply_limited_billing_rejects_manual_state_even_with_previous_restore_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = make_limited_snapshot()
    overlay = make_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.ACTIVE,
            expire_at=previous.expire_at,
            traffic_limit_bytes=previous.traffic_limit_bytes,
            squad_uuids=(OTHER_SQUAD,),
        )
    )
    install_fake_api(monkeypatch, api)

    with pytest.raises(GracePanelTransitionConflict, match='changed outside grace'):
        await RemnawaveGracePanelGateway().apply_billing_state(
            replace(make_limited_billing(), traffic_limit_bytes=5 * GIB),
            expected_overlay=overlay,
            expected_restored_snapshot=previous,
        )

    assert api.updates == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('status', 'squad_uuids'),
    [
        (UserStatus.LIMITED, (REGULAR_SQUAD,)),
        (UserStatus.ACTIVE, (GRACE_SQUAD,)),
    ],
)
@pytest.mark.parametrize(
    ('current_reset_at', 'current_used_traffic'),
    [
        (NOW, 10 * GIB),
        (NOW - timedelta(days=10), 9 * GIB),
    ],
)
async def test_apply_limited_billing_rejects_stale_restore_proof_after_reset_or_usage_drop(
    monkeypatch: pytest.MonkeyPatch,
    status: UserStatus,
    squad_uuids: tuple[str, ...],
    current_reset_at: datetime,
    current_used_traffic: int,
) -> None:
    previous_reset_at = NOW - timedelta(days=10)
    previous = replace(
        make_limited_snapshot(),
        last_traffic_reset_at=previous_reset_at,
    )
    overlay = make_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=status,
            expire_at=previous.expire_at,
            traffic_limit_bytes=previous.traffic_limit_bytes,
            squad_uuids=squad_uuids,
            external_squad_uuid=(
                previous.external_squad_uuid if status is UserStatus.LIMITED else overlay.external_squad_uuid
            ),
        )
    )
    api.user.last_traffic_reset_at = current_reset_at
    api.user.used_traffic_bytes = current_used_traffic
    api.user.user_traffic.used_traffic_bytes = current_used_traffic
    install_fake_api(monkeypatch, api)

    with pytest.raises(GracePanelTransitionConflict, match='changed outside grace'):
        await RemnawaveGracePanelGateway().apply_billing_state(
            replace(
                make_limited_billing(),
                traffic_limit_bytes=5 * GIB,
                squad_uuids=(OTHER_SQUAD,),
            ),
            expected_overlay=overlay,
            expected_restored_snapshot=previous,
        )

    assert api.updates == []


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
        await gateway.restore_snapshot(PANEL_ID, snapshot, overlay)

    assert_no_derived_status_writes(api)
    assert api.user.status is UserStatus.ACTIVE
    assert api.user.expire_at == snapshot.expire_at
    assert api.user.traffic_limit_bytes == snapshot.traffic_limit_bytes
    assert api.user.active_internal_squads == [{'uuid': GRACE_SQUAD}]
    assert api.user.external_squad_uuid is None

    api.user.status = UserStatus.LIMITED
    outcome = await gateway.restore_snapshot(PANEL_ID, snapshot, overlay)

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
        PANEL_ID,
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
    # Ровно один PATCH и ровно по числовому идентификатору: тело с ключом
    # uuid панель 3.0.0 отвергает (в схеме запроса такого поля нет).
    assert api.updates == [
        {
            'user_id': PANEL_ID,
            'hwid_device_limit': billing.device_limit,
        }
    ]
    assert_no_derived_status_writes(api)


@pytest.mark.asyncio
async def test_apply_active_billing_status_remains_one_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = make_overlay()
    billing = GraceBillingState(
        subscription_id=42,
        remnawave_id=PANEL_ID,
        status='active',
        end_at=NOW + timedelta(days=20),
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
    assert api.updates[0]['status'] is UserStatus.ACTIVE
    assert api.updates[0]['user_id'] == PANEL_ID
    assert api.disable_calls == []
    assert_no_derived_status_writes(api)
    assert api.user.active_internal_squads == [{'uuid': REGULAR_SQUAD}]
    assert api.user.external_squad_uuid == EXTERNAL_SQUAD


@pytest.mark.asyncio
async def test_apply_overlay_detaches_external_squad_first_and_addresses_the_numeric_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = make_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.EXPIRED,
            expire_at=NOW - timedelta(days=1),
            traffic_limit_bytes=10 * GIB,
            squad_uuids=(REGULAR_SQUAD,),
            external_squad_uuid=EXTERNAL_SQUAD,
        )
    )
    install_fake_api(monkeypatch, api)

    await RemnawaveGracePanelGateway().apply_overlay(PANEL_ID, overlay)

    # Отцепление внешнего сквада — отдельный первый PATCH: ретрай A039 без
    # externalSquadUuid не должен случайно выдать неограниченный доступ.
    assert api.updates[0] == {'user_id': PANEL_ID, 'external_squad_uuid': None}
    assert [update['user_id'] for update in api.updates] == [PANEL_ID, PANEL_ID]
    assert api.updates[1]['status'] is UserStatus.ACTIVE
    assert api.user.active_internal_squads == [{'uuid': GRACE_SQUAD}]
    assert api.user.external_squad_uuid is None


@pytest.mark.asyncio
async def test_read_snapshot_returns_the_numeric_panel_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.LIMITED,
            expire_at=NOW + timedelta(days=20),
            traffic_limit_bytes=10 * GIB,
            squad_uuids=(REGULAR_SQUAD,),
            external_squad_uuid=EXTERNAL_SQUAD,
        )
    )
    install_fake_api(monkeypatch, api)

    snapshot = await RemnawaveGracePanelGateway().read_snapshot(PANEL_ID)

    assert snapshot is not None
    assert snapshot.remnawave_id == PANEL_ID
    assert api.reads == [PANEL_ID]


@pytest.mark.asyncio
async def test_read_snapshot_rejects_a_legacy_uuid_instead_of_reporting_no_panel_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Маршруты 3.0.0 параметризованы числом, поэтому uuid даёт 400, а не 404.
    # Ответить на это None значило бы «панельного юзера нет» — и вызывающий
    # завёл бы дубль вместо того, чтобы починить битую связь в наших данных.
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.ACTIVE,
            expire_at=NOW + timedelta(days=20),
            traffic_limit_bytes=10 * GIB,
            squad_uuids=(REGULAR_SQUAD,),
        )
    )
    install_fake_api(monkeypatch, api)

    with pytest.raises(RemnaWaveInvalidUserIdError):
        await RemnawaveGracePanelGateway().read_snapshot(LEGACY_PANEL_UUID)

    assert api.updates == []


def test_v2_snapshot_row_stays_readable_after_the_identity_backfill() -> None:
    # list_open молча выбрасывает сессии, которые не смог разобрать. Откажись
    # ридер от v2 — такие оверлеи остались бы открытыми навсегда и без ошибки.
    session = _model_to_session(make_v2_session_row())

    assert session.remnawave_id == PANEL_ID
    # panel_before знает только исторический uuid, поэтому числовой id берётся
    # из колонки строки — бэкфил заполнил её из той же подписки.
    assert session.panel_before.remnawave_id == PANEL_ID
    assert session.panel_before.external_squad_uuid == EXTERNAL_SQUAD
    # В billing_before идентичность ни на одно решение не влияет, а лежавший там
    # uuid в 3.0.0 непригоден — значит None, а не строка 'aaaaaaaa-...'.
    assert session.billing_before.remnawave_id is None
    assert session.billing_before.device_limit == 4
    assert session.overlay.squad_uuids == (GRACE_SQUAD,)
    assert session.state is GraceSessionState.ACTIVE
    assert session.reason is GraceReason.EXPIRED


def test_v2_row_without_a_backfilled_id_fails_loudly_instead_of_closing_silently() -> None:
    # Пустая колонка — разорванная связь в наших данных, а не «в панели юзера
    # нет»: закрыть такую сессию без отката оверлея нельзя.
    with pytest.raises(GraceSnapshotError, match='remnawave_id'):
        _model_to_session(make_v2_session_row(remnawave_id=None))


def test_unsupported_snapshot_version_is_rejected_instead_of_guessed() -> None:
    row = make_v2_session_row()
    row.snapshot_version = 1

    with pytest.raises(GraceSnapshotError, match='Unsupported grace snapshot version'):
        _model_to_session(row)


def test_saving_a_v2_row_upgrades_it_to_v3_without_erasing_the_historical_uuid() -> None:
    session = _model_to_session(make_v2_session_row())

    values = _session_values(session)

    assert values['snapshot_version'] == 3
    assert values['remnawave_id'] == PANEL_ID
    # UPDATE не должен трогать историческую колонку: новый код uuid не знает, и
    # запись None стёрла бы единственный аудиторский след доапгрейдной сессии.
    assert 'remnawave_uuid' not in values

    upgraded = _model_to_session(_session_to_model(session))

    assert upgraded.remnawave_id == PANEL_ID
    assert upgraded.panel_before.remnawave_id == PANEL_ID


# ---- create_panel_user_grace_safe: подхват обязан ПРИМЕНИТЬ payload ----


@pytest.mark.asyncio
async def test_adopt_or_create_patches_the_adopted_panel_user(monkeypatch):
    """Подхватить аккаунт мало — вызывающий просил привести панель к состоянию.

    Регрессия: хелпер возвращал найденного пользователя без PATCH, поэтому
    админское «продлить»/«синхронизировать в панель» рапортовало успех, а в
    панели оставались старые статус, дата и лимиты.
    """
    from app.services.grace_access_runtime import _adopt_or_create

    adopted = SimpleNamespace(id=8812)
    patched = SimpleNamespace(id=8812)
    api = AsyncMock()
    api.get_user_by_short_uuid.return_value = adopted
    api.update_user.return_value = patched

    result = await _adopt_or_create(
        api, 'aBcD12', {'username': 'user_1_abc', 'status': 'ACTIVE', 'traffic_limit_bytes': 42}
    )

    assert result is patched
    api.create_user.assert_not_awaited()
    kwargs = api.update_user.await_args.kwargs
    assert kwargs['user_id'] == 8812
    assert kwargs['traffic_limit_bytes'] == 42
    assert 'username' not in kwargs, 'username — create-only, переименовывать аккаунт нельзя'


@pytest.mark.asyncio
async def test_adopt_or_create_creates_when_panel_denies_the_short_uuid(monkeypatch):
    from app.services.grace_access_runtime import _adopt_or_create

    created = SimpleNamespace(id=9001)
    api = AsyncMock()
    api.get_user_by_short_uuid.return_value = None
    api.create_user.return_value = created

    result = await _adopt_or_create(api, 'gone', {'username': 'u', 'status': 'ACTIVE'})

    assert result is created
    api.update_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_adopt_or_create_propagates_a_non_404_panel_error(monkeypatch):
    """Проглотить 5xx и создать нового — это и есть дубль рядом с живым аккаунтом."""
    from app.external.remnawave_api import RemnaWaveAPIError
    from app.services.grace_access_runtime import _adopt_or_create

    api = AsyncMock()
    api.get_user_by_short_uuid.side_effect = RemnaWaveAPIError('Bad Gateway', 502, {})

    with pytest.raises(RemnaWaveAPIError):
        await _adopt_or_create(api, 'aBcD12', {'username': 'u'})

    api.create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_adopt_does_not_wipe_squads_when_the_local_list_is_empty():
    """Пустой список сквадов НЕ должен уходить в PATCH.

    `create_user` пропускает пустой список, `update_user` — только None, а в
    контракте «не прислать» = не трогать, «прислать []» = снять все сквады.
    Переслав create-тело как есть, подхват снимал у живого оплаченного
    аккаунта все инбаунды: он оставался ACTIVE, но ссылка на подписку отдавала
    ноль конфигов. Состояние достижимо после «сброса подписки» и после
    удаления сквада из панели.
    """
    from app.services.grace_access_runtime import _adopt_or_create

    api = AsyncMock()
    api.get_user_by_short_uuid.return_value = SimpleNamespace(id=8812)
    api.update_user.return_value = SimpleNamespace(id=8812)

    await _adopt_or_create(api, 'aBcD12', {'username': 'u', 'status': 'ACTIVE', 'active_internal_squads': []})

    kwargs = api.update_user.await_args.kwargs
    assert 'active_internal_squads' not in kwargs


@pytest.mark.asyncio
async def test_adopt_forwards_a_non_empty_squad_list():
    """Обратная сторона: реальный список обязан доехать."""
    from app.services.grace_access_runtime import _adopt_or_create

    api = AsyncMock()
    api.get_user_by_short_uuid.return_value = SimpleNamespace(id=8812)
    api.update_user.return_value = SimpleNamespace(id=8812)

    await _adopt_or_create(api, 'aBcD12', {'username': 'u', 'active_internal_squads': ['squad-1', 'squad-2']})

    assert api.update_user.await_args.kwargs['active_internal_squads'] == ['squad-1', 'squad-2']


@pytest.mark.asyncio
async def test_open_session_without_panel_id_is_repaired_from_the_subscription(monkeypatch):
    """Сессия с пустым `remnawave_id` должна чиниться, а не жить вечно.

    Такую строку оставил старый код: колонки тогда не было. Читать её нельзя —
    `_model_to_session` бросает `GraceSnapshotError`, `get_open` роняет продление
    и разбор платежа, фоновой цикл пишет ошибку каждый проход, а уникальный
    индекс на открытую сессию не даёт открыть новую. При этом ответ лежит рядом:
    подписка уже связана бэкфилом.
    """
    from app.database.models import Subscription as SubModel, User as UserModel
    from app.services.grace_access_runtime import SQLAlchemyGraceSessionStore
    from tests.fixtures.sqlite_memory import memory_session

    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        db.add(UserModel(id=1, telegram_id=100, remnawave_id=None))
        db.add(
            SubModel(
                id=42,
                user_id=1,
                status='expired',
                end_date=NOW - timedelta(days=1),
                remnawave_id=PANEL_ID,
                remnawave_short_id='sid42',
            )
        )
        # Ровно то, что оставил старый код: валидный снапшот, но колонка пуста.
        db.add(make_v2_session_row(remnawave_id=None))
        await db.commit()

        store = SQLAlchemyGraceSessionStore(db)
        sessions = await store.list_open(limit=10)

        assert len(sessions) == 1, 'сессия должна стать читаемой, а не пропускаться каждый цикл'
        assert sessions[0].remnawave_id == PANEL_ID

        db.expunge_all()
        row = await db.get(GraceAccessSessionModel, '11111111-2222-3333-4444-555555555555')
        assert row.remnawave_id == PANEL_ID, 'починка должна сохраниться, а не повторяться каждый проход'


@pytest.mark.asyncio
async def test_get_open_no_longer_explodes_on_a_session_without_panel_id(monkeypatch):
    """`get_open` вызывают продление и разбор платежа — он не должен падать."""
    from app.database.models import Subscription as SubModel, User as UserModel
    from app.services.grace_access_runtime import SQLAlchemyGraceSessionStore
    from tests.fixtures.sqlite_memory import memory_session

    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        db.add(UserModel(id=1, telegram_id=100, remnawave_id=None))
        db.add(
            SubModel(
                id=42,
                user_id=1,
                status='expired',
                end_date=NOW - timedelta(days=1),
                remnawave_id=PANEL_ID,
                remnawave_short_id='sid42',
            )
        )
        db.add(make_v2_session_row(remnawave_id=None))
        await db.commit()

        session = await SQLAlchemyGraceSessionStore(db).get_open(42)

        assert session is not None
        assert session.remnawave_id == PANEL_ID


@pytest.mark.asyncio
async def test_apply_future_disabled_billing_uses_action_then_field_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = make_overlay()
    canonical_expire_at = NOW + timedelta(days=20)
    billing = GraceBillingState(
        subscription_id=42,
        remnawave_id=PANEL_ID,
        status='disabled',
        end_at=canonical_expire_at,
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

    assert api.disable_calls == [PANEL_ID]
    assert len(api.updates) == 1
    assert 'status' not in api.updates[0]
    assert api.updates[0]['expire_at'] == canonical_expire_at
    assert api.user.status is UserStatus.DISABLED
    assert api.user.expire_at == canonical_expire_at
    assert api.user.active_internal_squads == [{'uuid': REGULAR_SQUAD}]
    assert api.user.external_squad_uuid == EXTERNAL_SQUAD


@pytest.mark.asyncio
async def test_apply_disabled_billing_from_limited_uses_disable_action_before_larger_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = make_overlay()
    billing = GraceBillingState(
        subscription_id=42,
        remnawave_id=PANEL_ID,
        status='disabled',
        end_at=NOW - timedelta(days=1),
        traffic_limit_bytes=20 * GIB,
        used_traffic_bytes=11 * GIB,
        device_limit=4,
        squad_uuids=(REGULAR_SQUAD,),
        external_squad_uuid=EXTERNAL_SQUAD,
    )
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

    assert api.disable_calls == [PANEL_ID]
    assert len(api.updates) == 1
    assert 'status' not in api.updates[0]
    assert 'expire_at' not in api.updates[0]
    assert api.user.status is UserStatus.DISABLED
    assert api.user.traffic_limit_bytes == billing.traffic_limit_bytes
