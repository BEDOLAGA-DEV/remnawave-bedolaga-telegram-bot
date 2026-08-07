from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.services.grace_access_service import (
    GraceAccessMode,
    GraceAccessPolicy,
    GraceAccessService,
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
    GraceStartDecision,
    GraceSubscriptionKind,
    GraceTrafficResetOutcome,
    GraceTrafficResetResult,
    billing_is_eligible,
    build_incident_key,
    build_tariff_rebase_lineage_key,
    classify_subscription_kind,
    tariff_rebase_lineage_blocks_new_grant,
)


GIB = 1024**3
EXPIRED_SQUAD = '11111111-1111-1111-1111-111111111111'
LIMITED_SQUAD = '22222222-2222-2222-2222-222222222222'
REGULAR_SQUAD = '33333333-3333-3333-3333-333333333333'
NEW_TARIFF_SQUAD = '44444444-4444-4444-4444-444444444444'
NEW_EXTERNAL_SQUAD = '55555555-5555-5555-5555-555555555555'
# Remnawave 3.0.0 идентифицирует панельного пользователя числовым id;
# поля uuid у записи больше нет.
PANEL_ID = 4242
OTHER_PANEL_ID = 7777


def test_runtime_mode_values_are_explicit_and_fail_closed() -> None:
    assert GraceAccessMode.parse('false') is GraceAccessMode.DISABLED
    assert GraceAccessMode.parse('observe') is GraceAccessMode.OBSERVE
    assert GraceAccessMode.parse('true') is GraceAccessMode.ACTIVE
    assert GraceAccessMode.parse('drain') is GraceAccessMode.DRAIN
    with pytest.raises(ValueError, match='must be one of'):
        GraceAccessMode.parse('active')
    with pytest.raises(ValueError, match='must be one of'):
        GraceAccessMode.parse('off')


class MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


class MemoryGraceStore:
    def __init__(self) -> None:
        self.sessions: dict[str, GraceAccessSession] = {}

    async def get_open(self, subscription_id: int) -> GraceAccessSession | None:
        return next(
            (
                session
                for session in self.sessions.values()
                if session.subscription_id == subscription_id and session.state is not GraceSessionState.COMPLETED
            ),
            None,
        )

    async def get_by_incident(self, subscription_id: int, incident_key: str) -> GraceAccessSession | None:
        matches: list[GraceAccessSession] = []
        for session in self.sessions.values():
            if session.subscription_id != subscription_id:
                continue
            legacy_lineage_key = (
                build_tariff_rebase_lineage_key(
                    session.billing_before,
                    session.reason,
                    last_traffic_reset_at=session.panel_before.last_traffic_reset_at,
                )
                if session.reason is GraceReason.LIMITED
                else None
            )
            if (
                session.incident_key == incident_key
                or incident_key in session.incident_aliases
                or incident_key == legacy_lineage_key
            ):
                matches.append(session)
        return max(matches, key=lambda session: (session.updated_at, session.version)) if matches else None

    async def create(self, session: GraceAccessSession) -> GraceAccessSession:
        self.sessions[session.id] = session
        return session

    async def save(self, session: GraceAccessSession) -> GraceAccessSession:
        self.sessions[session.id] = session
        return session

    async def checkpoint(self, session: GraceAccessSession) -> GraceAccessSession:
        return await self.save(session)

    async def list_open(self, *, limit: int) -> list[GraceAccessSession]:
        sessions = [session for session in self.sessions.values() if session.state is not GraceSessionState.COMPLETED]
        return sessions[:limit]

    async def list_recent_completed(
        self,
        subscription_id: int,
        *,
        limit: int = 8,
    ) -> list[GraceAccessSession]:
        sessions = [
            session
            for session in self.sessions.values()
            if session.subscription_id == subscription_id
            and session.state is GraceSessionState.COMPLETED
            and session.completed_at is not None
        ]
        sessions.sort(key=lambda session: session.completed_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return sessions[:limit]

    def only_session(self) -> GraceAccessSession:
        assert len(self.sessions) == 1
        return next(iter(self.sessions.values()))


class FakePanelGateway:
    def __init__(self, snapshot: GracePanelSnapshot) -> None:
        self.snapshot = snapshot
        self.applied_overlays: list[tuple[int, GracePanelOverlay]] = []
        self.restored_snapshots: list[tuple[int, GracePanelSnapshot]] = []
        self.applied_billing: list[GraceBillingState] = []
        self.applied_billing_overlays: list[GracePanelOverlay] = []
        self.fail_overlay_attempts = 0
        self.conflict_billing_attempts = 0
        self.pending_billing_attempts = 0
        self.pending_restore_attempts = 0
        self.restore_outcome = GraceRestoreOutcome.RESTORED
        self.restore_force_flags: list[bool] = []
        self.missing_billing_revocations: list[GracePanelOverlay] = []
        self.external_reset_outcome = GraceRestoreOutcome.RESTORED
        self.external_reset_revocations: list[tuple[GracePanelOverlay, datetime | None, datetime | None]] = []
        self.restore_state_probe: Any = None
        self.observed_restore_states: list[GraceSessionState] = []
        self.prepared_tariff_rebases: list[GraceBillingState] = []
        self.traffic_reset_calls: list[GraceBillingState] = []
        self.traffic_reset_effect_applied = False
        self.traffic_reset_effects = 0
        self.fail_traffic_reset_after_effect = 0

    async def read_snapshot(self, remnawave_id: int) -> GracePanelSnapshot | None:
        if remnawave_id != self.snapshot.remnawave_id:
            return None
        return self.snapshot

    async def apply_overlay(
        self,
        remnawave_id: int,
        overlay: GracePanelOverlay,
        *,
        expected_source: GracePanelSnapshot,
    ) -> None:
        assert expected_source.remnawave_id == remnawave_id
        if self.fail_overlay_attempts > 0:
            self.fail_overlay_attempts -= 1
            raise RuntimeError('temporary panel error')
        self.applied_overlays.append((remnawave_id, overlay))
        self.snapshot = GracePanelSnapshot(
            remnawave_id=remnawave_id,
            status=overlay.status,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            used_traffic_bytes=self.snapshot.used_traffic_bytes,
            squad_uuids=overlay.squad_uuids,
            external_squad_uuid=overlay.external_squad_uuid,
            traffic_is_known=self.snapshot.traffic_is_known,
            last_traffic_reset_at=self.snapshot.last_traffic_reset_at,
            traffic_limit_strategy=overlay.traffic_limit_strategy,
        )

    async def restore_snapshot(
        self,
        remnawave_id: int,
        snapshot: GracePanelSnapshot,
        expected_overlay: GracePanelOverlay,
        *,
        force_disable: bool = False,
    ) -> GraceRestoreOutcome:
        self.restore_force_flags.append(force_disable)
        if self.restore_state_probe is not None:
            self.observed_restore_states.append(self.restore_state_probe())
        self.restored_snapshots.append((remnawave_id, snapshot))
        if self.pending_restore_attempts > 0:
            self.pending_restore_attempts -= 1
            raise GracePanelTransitionPending
        if self.restore_outcome is GraceRestoreOutcome.RESTORED:
            self.snapshot = replace(
                snapshot,
                # Consumed traffic is accounting data and is never restored.
                used_traffic_bytes=self.snapshot.used_traffic_bytes,
            )
        return self.restore_outcome

    async def revoke_missing_billing(
        self,
        remnawave_id: int,
        *,
        expected_overlay: GracePanelOverlay,
    ) -> None:
        assert remnawave_id == self.snapshot.remnawave_id
        self.missing_billing_revocations.append(expected_overlay)
        self.snapshot = replace(self.snapshot, status='DISABLED')

    async def fail_closed_external_reset(
        self,
        remnawave_id: int,
        *,
        expected_overlay: GracePanelOverlay,
        expected_last_traffic_reset_at: datetime | None,
        observed_last_traffic_reset_at: datetime | None,
    ) -> GraceRestoreOutcome:
        assert remnawave_id == self.snapshot.remnawave_id
        self.external_reset_revocations.append(
            (
                expected_overlay,
                expected_last_traffic_reset_at,
                observed_last_traffic_reset_at,
            )
        )
        if self.external_reset_outcome is not GraceRestoreOutcome.CONFLICT:
            self.snapshot = replace(self.snapshot, status='DISABLED')
        return self.external_reset_outcome

    async def apply_billing_state(
        self,
        billing: GraceBillingState,
        *,
        expected_overlay: GracePanelOverlay,
        expected_restored_snapshot: GracePanelSnapshot | None = None,
        require_overlay_source: bool = False,
        expected_last_traffic_reset_at: datetime | None = None,
    ) -> None:
        self.applied_billing.append(billing)
        self.applied_billing_overlays.append(expected_overlay)
        if self.conflict_billing_attempts > 0:
            self.conflict_billing_attempts -= 1
            raise GracePanelTransitionConflict('panel state changed outside grace')
        if self.pending_billing_attempts > 0:
            self.pending_billing_attempts -= 1
            raise GracePanelTransitionPending

    async def prepare_tariff_rebase(
        self,
        billing: GraceBillingState,
        *,
        expected_overlay: GracePanelOverlay,
        expected_last_traffic_reset_at: datetime | None,
    ) -> GracePanelSnapshot | None:
        self.prepared_tariff_rebases.append(billing)
        status = str(self.snapshot.status).strip().lower().rsplit('.', maxsplit=1)[-1]
        expiry_matches = (
            self.snapshot.expire_at is not None
            and abs((self.snapshot.expire_at - expected_overlay.expire_at).total_seconds()) <= 2
        )
        if not (
            status in {'active', 'limited'}
            and expiry_matches
            and self.snapshot.traffic_limit_bytes == expected_overlay.traffic_limit_bytes
            and set(self.snapshot.squad_uuids) == set(expected_overlay.squad_uuids)
            and self.snapshot.external_squad_uuid == expected_overlay.external_squad_uuid
            and self.snapshot.last_traffic_reset_at == expected_last_traffic_reset_at
        ):
            return None
        return self.snapshot

    async def apply_tariff_switch_traffic_reset(
        self,
        billing: GraceBillingState,
        *,
        reason: GraceReason,
        expected_overlay: GracePanelOverlay,
        expected_last_traffic_reset_at: datetime | None,
        remaining_grace_bytes: int,
    ) -> GraceTrafficResetResult:
        self.traffic_reset_calls.append(billing)
        reset_at = (
            expected_last_traffic_reset_at + timedelta(seconds=1)
            if expected_last_traffic_reset_at is not None
            else datetime(2026, 7, 15, 12, 0, 1, tzinfo=UTC)
        )
        if not self.traffic_reset_effect_applied:
            self.traffic_reset_effect_applied = True
            self.traffic_reset_effects += 1
            self.snapshot = replace(
                self.snapshot,
                status='ACTIVE',
                used_traffic_bytes=0,
                last_traffic_reset_at=reset_at,
            )
            if self.fail_traffic_reset_after_effect > 0:
                self.fail_traffic_reset_after_effect -= 1
                raise RuntimeError('lost reset response')

        if reason is GraceReason.LIMITED and billing.end_at is not None:
            self.snapshot = replace(
                self.snapshot,
                status='ACTIVE',
                expire_at=billing.end_at,
                traffic_limit_bytes=billing.traffic_limit_bytes,
                squad_uuids=billing.squad_uuids,
                external_squad_uuid=billing.external_squad_uuid,
            )
            return GraceTrafficResetResult(
                GraceTrafficResetOutcome.RECOVERED,
                self.snapshot,
            )
        if remaining_grace_bytes == 0:
            self.snapshot = replace(
                self.snapshot,
                status='DISABLED',
                traffic_limit_bytes=billing.traffic_limit_bytes,
                squad_uuids=billing.squad_uuids,
                external_squad_uuid=billing.external_squad_uuid,
            )
            return GraceTrafficResetResult(
                GraceTrafficResetOutcome.EXHAUSTED,
                self.snapshot,
            )
        overlay = replace(
            expected_overlay,
            traffic_limit_bytes=remaining_grace_bytes,
        )
        self.snapshot = replace(
            self.snapshot,
            status='ACTIVE',
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
            external_squad_uuid=overlay.external_squad_uuid,
        )
        return GraceTrafficResetResult(
            GraceTrafficResetOutcome.CONTINUED,
            self.snapshot,
            overlay=overlay,
        )


class FakeBillingGateway:
    def __init__(self, state: GraceBillingState) -> None:
        self.state = state
        self.queued_states: list[GraceBillingState | None] = []
        self.get_calls = 0
        self.fail_on_get_call: int | None = None

    async def get_subscription(self, subscription_id: int) -> GraceBillingState | None:
        self.get_calls += 1
        if self.get_calls == self.fail_on_get_call:
            raise RuntimeError('lost canonical read after reset')
        if self.queued_states:
            queued = self.queued_states.pop(0)
            if queued is not None:
                self.state = queued
            return queued
        if subscription_id != self.state.subscription_id:
            return None
        return self.state

    async def mark_active_after_traffic_reset(
        self,
        expected: GraceBillingState,
    ) -> GraceBillingState | None:
        if self.state.subscription_id != expected.subscription_id:
            return None
        self.state = replace(self.state, status='active')
        return self.state


def make_billing(
    *,
    status: str,
    end_at: datetime,
    traffic_limit_bytes: int = 10 * GIB,
    used_traffic_bytes: int = 3 * GIB,
    tariff_id: int | None = 1,
    tariff_id_known: bool = True,
) -> GraceBillingState:
    return GraceBillingState(
        subscription_id=42,
        remnawave_id=PANEL_ID,
        status=status,
        end_at=end_at,
        traffic_limit_bytes=traffic_limit_bytes,
        used_traffic_bytes=used_traffic_bytes,
        device_limit=2,
        squad_uuids=(REGULAR_SQUAD,),
        traffic_limit_strategy='MONTH',
        tariff_id=tariff_id,
        tariff_id_known=tariff_id_known,
    )


def make_snapshot(
    *,
    expire_at: datetime,
    traffic_limit_bytes: int = 10 * GIB,
    used_traffic_bytes: int = 3 * GIB,
) -> GracePanelSnapshot:
    return GracePanelSnapshot(
        remnawave_id=PANEL_ID,
        status='DISABLED',
        expire_at=expire_at,
        traffic_limit_bytes=traffic_limit_bytes,
        used_traffic_bytes=used_traffic_bytes,
        squad_uuids=(REGULAR_SQUAD,),
        traffic_limit_strategy='MONTH',
    )


def make_policy(**changes) -> GraceAccessPolicy:
    policy = GraceAccessPolicy(
        duration=timedelta(days=3),
        expired_squad_uuid=EXPIRED_SQUAD,
        limited_squad_uuid=LIMITED_SQUAD,
        traffic_bytes=GIB,
    )
    return replace(policy, **changes)


def make_restore_modified_echo(session: GraceAccessSession) -> dict[str, object]:
    echo_at = session.restore_finished_at or session.restore_started_at or session.updated_at
    return {
        'id': session.remnawave_id,
        'status': 'EXPIRED',
        'updatedAt': echo_at.isoformat(),
        'expireAt': session.overlay.expire_at.isoformat(),
        'trafficLimitBytes': session.panel_before.traffic_limit_bytes,
        'trafficLimitStrategy': session.panel_before.traffic_limit_strategy,
        'activeInternalSquads': [{'uuid': squad_uuid} for squad_uuid in session.panel_before.squad_uuids],
        'externalSquadUuid': session.panel_before.external_squad_uuid,
        'lastTrafficResetAt': (
            session.panel_before.last_traffic_reset_at.isoformat()
            if session.panel_before.last_traffic_reset_at is not None
            else None
        ),
        'hwidDeviceLimit': session.billing_before.device_limit,
        'userTraffic': {'usedTrafficBytes': session.panel_before.used_traffic_bytes},
    }


def make_service(
    *,
    billing: GraceBillingState,
    snapshot: GracePanelSnapshot,
    clock: MutableClock,
    policy: GraceAccessPolicy | None = None,
) -> tuple[GraceAccessService, MemoryGraceStore, FakePanelGateway, FakeBillingGateway]:
    store = MemoryGraceStore()
    panel = FakePanelGateway(snapshot)
    billing_gateway = FakeBillingGateway(billing)
    service = GraceAccessService(
        store=store,
        panel=panel,
        billing=billing_gateway,
        policy=policy or make_policy(),
        clock=clock,
    )
    return service, store, panel, billing_gateway


def test_subscription_kind_priority_and_feature_flags() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    regular = make_billing(status='expired', end_at=now)
    trial = replace(regular, is_trial=True)
    daily = replace(regular, is_daily=True)
    free = replace(regular, is_free_tariff=True)
    overlapping = replace(regular, is_trial=True, is_daily=True, is_free_tariff=True)

    assert classify_subscription_kind(regular) is GraceSubscriptionKind.REGULAR_PAID
    assert classify_subscription_kind(trial) is GraceSubscriptionKind.TRIAL
    assert classify_subscription_kind(daily) is GraceSubscriptionKind.DAILY
    assert classify_subscription_kind(free) is GraceSubscriptionKind.FREE
    assert classify_subscription_kind(overlapping) is GraceSubscriptionKind.TRIAL

    default_policy = make_policy()
    assert billing_is_eligible(regular, GraceReason.EXPIRED, default_policy) is True
    assert billing_is_eligible(trial, GraceReason.EXPIRED, default_policy) is False
    assert billing_is_eligible(daily, GraceReason.EXPIRED, default_policy) is False
    assert billing_is_eligible(free, GraceReason.EXPIRED, default_policy) is False

    enabled_policy = make_policy(trial_enabled=True, daily_enabled=True, free_enabled=True)
    assert billing_is_eligible(trial, GraceReason.EXPIRED, enabled_policy) is True
    assert billing_is_eligible(daily, GraceReason.EXPIRED, enabled_policy) is True
    assert billing_is_eligible(free, GraceReason.EXPIRED, enabled_policy) is True
    assert billing_is_eligible(overlapping, GraceReason.EXPIRED, make_policy(daily_enabled=True)) is False
    assert billing_is_eligible(overlapping, GraceReason.EXPIRED, make_policy(trial_enabled=True)) is True


def test_limited_incident_key_tracks_end_limit_and_reset_timestamp() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    billing = make_billing(status='limited', end_at=now + timedelta(days=30))
    unknown = build_incident_key(billing, GraceReason.LIMITED)

    assert unknown.endswith(':unknown')
    assert build_incident_key(billing, GraceReason.LIMITED) == unknown
    assert (
        build_incident_key(replace(billing, end_at=billing.end_at + timedelta(days=30)), GraceReason.LIMITED) != unknown
    )
    assert build_incident_key(replace(billing, traffic_limit_bytes=20 * GIB), GraceReason.LIMITED) != unknown
    assert (
        build_incident_key(
            billing,
            GraceReason.LIMITED,
            last_traffic_reset_at=now,
        )
        != unknown
    )


@pytest.mark.asyncio
async def test_expired_grace_changes_only_panel_overlay() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = make_snapshot(expire_at=billing.end_at)
    service, store, panel, _ = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )

    result = await service.start_if_eligible(billing, GraceReason.EXPIRED)

    assert result.decision is GraceStartDecision.STARTED
    assert result.session is not None
    assert result.session.state is GraceSessionState.ACTIVE
    assert result.session.billing_before is billing
    assert result.session.panel_before is snapshot
    assert result.session.overlay.status == 'ACTIVE'
    assert result.session.overlay.expire_at == now + timedelta(days=3)
    assert result.session.overlay.traffic_limit_bytes == snapshot.used_traffic_bytes + GIB
    assert result.session.overlay.squad_uuids == (EXPIRED_SQUAD,)
    assert len(panel.applied_overlays) == 1
    assert store.only_session().state is GraceSessionState.ACTIVE


@pytest.mark.asyncio
async def test_limited_grace_adds_bytes_above_usage_without_resetting_usage() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='LIMITED',
    )
    service, _, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)

    result = await service.start_if_eligible(billing, GraceReason.LIMITED)

    assert result.session is not None
    assert result.session.overlay.expire_at == now + timedelta(days=3)
    assert result.session.overlay.traffic_limit_bytes == 11 * GIB
    assert result.session.overlay.squad_uuids == (LIMITED_SQUAD,)
    assert result.session.panel_before.expire_at == now + timedelta(days=20)
    assert result.session.panel_before.used_traffic_bytes == 10 * GIB
    assert panel.restored_snapshots == []


@pytest.mark.asyncio
async def test_same_incident_is_not_granted_twice() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = make_snapshot(expire_at=billing.end_at)
    service, _, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)

    first = await service.start_if_eligible(billing, GraceReason.EXPIRED)
    second = await service.start_if_eligible(billing, GraceReason.EXPIRED)

    assert first.decision is GraceStartDecision.STARTED
    assert second.decision is GraceStartDecision.ALREADY_ACTIVE
    assert len(panel.applied_overlays) == 1


@pytest.mark.asyncio
async def test_pending_session_retries_same_overlay_after_temporary_error() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = make_snapshot(expire_at=billing.end_at)
    service, store, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    panel.fail_overlay_attempts = 1

    with pytest.raises(RuntimeError, match='temporary panel error'):
        await service.start_if_eligible(billing, GraceReason.EXPIRED)

    assert store.only_session().state is GraceSessionState.PENDING
    assert store.only_session().last_error == 'RuntimeError: temporary panel error'

    retried = await service.start_if_eligible(billing, GraceReason.EXPIRED)

    assert retried.decision is GraceStartDecision.RETRIED
    assert retried.session is not None
    assert retried.session.state is GraceSessionState.ACTIVE
    assert len(panel.applied_overlays) == 1


@pytest.mark.asyncio
async def test_pending_retry_accepts_only_known_external_squad_detach_intermediate() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    external_squad = '44444444-4444-4444-4444-444444444444'
    billing = replace(
        make_billing(status='expired', end_at=now - timedelta(minutes=1)),
        external_squad_uuid=external_squad,
    )
    snapshot = replace(
        make_snapshot(expire_at=billing.end_at),
        status='EXPIRED',
        external_squad_uuid=external_squad,
    )
    service, store, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    panel.fail_overlay_attempts = 1

    with pytest.raises(RuntimeError, match='temporary panel error'):
        await service.start_if_eligible(billing, GraceReason.EXPIRED)

    # This is the only supported partial PATCH: external squad detached while
    # every other controlled value is still the original snapshot.
    panel.snapshot = replace(panel.snapshot, external_squad_uuid=None)
    retried = await service.start_if_eligible(billing, GraceReason.EXPIRED)

    assert retried.decision is GraceStartDecision.RETRIED
    assert store.only_session().state is GraceSessionState.ACTIVE
    assert len(panel.applied_overlays) == 1


@pytest.mark.asyncio
async def test_pending_unexpected_active_keeps_protection_open_without_reapplying_overlay() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(minutes=1))
    snapshot = replace(make_snapshot(expire_at=billing.end_at), status='EXPIRED')
    service, store, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    panel.fail_overlay_attempts = 1

    with pytest.raises(RuntimeError, match='temporary panel error'):
        await service.start_if_eligible(billing, GraceReason.EXPIRED)

    panel.snapshot = replace(
        panel.snapshot,
        status='ACTIVE',
        expire_at=now + timedelta(days=30),
    )
    result = await service.reconcile()

    assert result.conflicts == 1
    assert panel.applied_overlays == []
    restoring = store.only_session()
    assert restoring.state is GraceSessionState.RESTORING
    assert restoring.completion_reason is None
    assert restoring.completed_at is None


@pytest.mark.asyncio
async def test_pending_retry_never_reenables_an_unexpected_manual_panel_state() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    external_squad = '44444444-4444-4444-4444-444444444444'
    billing = replace(
        make_billing(status='expired', end_at=now - timedelta(minutes=1)),
        external_squad_uuid=external_squad,
    )
    snapshot = replace(
        make_snapshot(expire_at=billing.end_at),
        status='EXPIRED',
        external_squad_uuid=external_squad,
    )
    service, store, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    panel.fail_overlay_attempts = 1

    with pytest.raises(RuntimeError, match='temporary panel error'):
        await service.start_if_eligible(billing, GraceReason.EXPIRED)

    # The preflight detach may have succeeded, but an administrator then
    # disabled the panel user. Retry must apply fail-closed billing, not ACTIVE.
    panel.snapshot = replace(panel.snapshot, status='DISABLED', external_squad_uuid=None)
    retried = await service.start_if_eligible(billing, GraceReason.EXPIRED)

    assert retried.decision is GraceStartDecision.SUPERSEDED
    assert store.only_session().state is GraceSessionState.COMPLETED
    assert store.only_session().completion_reason is GraceCompletionReason.CONFLICT
    assert panel.applied_overlays == []
    assert panel.applied_billing == [billing]


@pytest.mark.asyncio
async def test_timeout_restores_original_panel_values_once() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = make_snapshot(expire_at=billing.end_at)
    service, store, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    await service.start_if_eligible(billing, GraceReason.EXPIRED)
    panel.snapshot = replace(panel.snapshot, used_traffic_bytes=7 * GIB)
    clock.advance(timedelta(days=3, seconds=1))

    first_reconcile = await service.reconcile()
    second_reconcile = await service.reconcile()

    assert first_reconcile.timed_out == 1
    assert second_reconcile.inspected == 0
    assert panel.restored_snapshots == [(PANEL_ID, snapshot)]
    completed = store.only_session()
    assert completed.state is GraceSessionState.COMPLETED
    assert completed.completion_reason is GraceCompletionReason.TIMEOUT
    assert panel.snapshot.used_traffic_bytes == 7 * GIB

    repeated = await service.start_if_eligible(billing, GraceReason.EXPIRED)
    assert repeated.decision is GraceStartDecision.ALREADY_GRANTED
    assert len(panel.applied_overlays) == 1


@pytest.mark.asyncio
async def test_limited_snapshot_restore_stays_restoring_while_panel_derives_status() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='LIMITED',
    )
    service, store, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    await service.start_if_eligible(billing, GraceReason.LIMITED)
    panel.pending_restore_attempts = 1
    panel.restore_state_probe = lambda: store.only_session().state
    clock.advance(timedelta(days=3, seconds=1))

    pending = await service.reconcile()

    assert pending.unchanged == 1
    assert pending.errors == 0
    assert pending.timed_out == 0
    assert store.only_session().state is GraceSessionState.RESTORING
    assert store.only_session().last_error is None
    assert panel.observed_restore_states == [GraceSessionState.RESTORING]

    completed = await service.reconcile()

    assert completed.timed_out == 1
    assert completed.errors == 0
    assert store.only_session().state is GraceSessionState.COMPLETED
    assert store.only_session().completion_reason is GraceCompletionReason.TIMEOUT
    assert panel.restored_snapshots == [
        (PANEL_ID, snapshot),
        (PANEL_ID, snapshot),
    ]
    assert panel.observed_restore_states == [
        GraceSessionState.RESTORING,
        GraceSessionState.RESTORING,
    ]


@pytest.mark.asyncio
async def test_tariff_change_while_limited_restore_is_pending_converges_to_new_tariff() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='LIMITED',
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    await service.start_if_eligible(billing, GraceReason.LIMITED)
    panel.pending_restore_attempts = 1
    clock.advance(timedelta(days=3, seconds=1))
    assert (await service.reconcile()).unchanged == 1
    assert store.only_session().state is GraceSessionState.RESTORING

    changed = replace(
        billing,
        tariff_id=2,
        traffic_limit_bytes=5 * GIB,
        squad_uuids=(NEW_TARIFF_SQUAD,),
    )
    billing_gateway.state = changed
    result = await service.reconcile()

    assert result.conflicts == 1
    assert panel.applied_billing == [changed]
    assert len(panel.restored_snapshots) == 1
    completed = store.only_session()
    assert completed.completion_reason is GraceCompletionReason.CONFLICT
    assert completed.limited_lineage_tail == changed


@pytest.mark.asyncio
async def test_payment_wins_over_grace_snapshot() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = make_snapshot(expire_at=billing.end_at)
    service, store, panel, billing_gateway = make_service(billing=billing, snapshot=snapshot, clock=clock)
    await service.start_if_eligible(billing, GraceReason.EXPIRED)
    paid_billing = replace(
        billing,
        status='active',
        end_at=now + timedelta(days=30),
        used_traffic_bytes=0,
    )
    billing_gateway.state = paid_billing

    result = await service.reconcile()

    assert result.paid == 1
    assert panel.applied_billing == [paid_billing]
    assert panel.restored_snapshots == []
    completed = store.only_session()
    assert completed.state is GraceSessionState.COMPLETED
    assert completed.completion_reason is GraceCompletionReason.PAID


@pytest.mark.asyncio
async def test_confirmed_panel_sync_can_finish_payment_without_duplicate_panel_update() -> None:
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = make_snapshot(expire_at=billing.end_at)
    service, store, panel, billing_gateway = make_service(billing=billing, snapshot=snapshot, clock=clock)
    await service.start_if_eligible(billing, GraceReason.EXPIRED)

    paid_billing = replace(
        billing,
        status='active',
        end_at=now + timedelta(days=30),
        used_traffic_bytes=0,
    )
    billing_gateway.state = paid_billing

    assert (
        await service.complete_after_payment(
            billing.subscription_id,
            apply_billing_state=False,
        )
        is True
    )
    assert panel.applied_billing == []
    completed = store.only_session()
    assert completed.state is GraceSessionState.COMPLETED
    assert completed.completion_reason is GraceCompletionReason.PAID


@pytest.mark.asyncio
async def test_limited_tariff_switch_rebases_active_grace_and_restores_latest_tariff() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    reset_at = now - timedelta(days=10)
    clock = MutableClock(now)
    billing = replace(
        make_billing(
            status='limited',
            end_at=now + timedelta(days=20),
            traffic_limit_bytes=10 * GIB,
            used_traffic_bytes=10 * GIB,
        ),
        traffic_limit_strategy='MONTH',
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='LIMITED',
        last_traffic_reset_at=reset_at,
        traffic_limit_strategy=billing.traffic_limit_strategy,
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    started = await service.start_if_eligible(billing, GraceReason.LIMITED)
    assert started.session is not None
    original = started.session

    changed_billing = replace(
        billing,
        tariff_id=2,
        traffic_limit_bytes=5 * GIB,
        used_traffic_bytes=10 * GIB + GIB // 2,
        device_limit=4,
        squad_uuids=(NEW_TARIFF_SQUAD,),
        external_squad_uuid=NEW_EXTERNAL_SQUAD,
        traffic_limit_strategy='DAY',
    )
    billing_gateway.state = changed_billing
    panel.snapshot = replace(
        panel.snapshot,
        used_traffic_bytes=changed_billing.used_traffic_bytes,
    )

    rebased_result = await service.reconcile()

    assert rebased_result.repaired == 1
    assert rebased_result.conflicts == 0
    rebased = store.only_session()
    assert rebased.id == original.id
    assert rebased.incident_key == original.incident_key
    assert rebased.started_at == original.started_at
    assert rebased.grace_until == original.grace_until
    assert rebased.overlay == original.overlay
    assert rebased.billing_before == changed_billing
    assert rebased.panel_before.status == snapshot.status
    assert rebased.panel_before.expire_at == changed_billing.end_at
    assert rebased.panel_before.traffic_limit_bytes == changed_billing.traffic_limit_bytes
    assert rebased.panel_before.squad_uuids == changed_billing.squad_uuids
    assert rebased.panel_before.external_squad_uuid == changed_billing.external_squad_uuid
    assert rebased.panel_before.traffic_limit_strategy == changed_billing.traffic_limit_strategy
    assert panel.applied_billing == []
    assert len(panel.applied_overlays) == 1
    assert panel.snapshot.expire_at == original.overlay.expire_at
    assert panel.snapshot.traffic_limit_bytes == original.overlay.traffic_limit_bytes
    assert panel.snapshot.squad_uuids == original.overlay.squad_uuids

    clock.advance(timedelta(days=3, seconds=1))
    timed_out = await service.reconcile()

    assert timed_out.timed_out == 1
    completed = store.only_session()
    assert completed.completion_reason is GraceCompletionReason.TIMEOUT
    assert panel.restored_snapshots[-1][1] == rebased.panel_before

    repeated = await service.start_if_eligible(changed_billing, GraceReason.LIMITED)
    assert repeated.decision is GraceStartDecision.ALREADY_GRANTED
    assert repeated.session is not None
    assert repeated.session.id == original.id

    next_reset = reset_at + timedelta(days=30)
    panel.snapshot = replace(panel.snapshot, last_traffic_reset_at=next_reset)
    next_period = await service.start_if_eligible(changed_billing, GraceReason.LIMITED)
    assert next_period.decision is GraceStartDecision.STARTED
    assert next_period.session is not None
    assert next_period.session.id != original.id


@pytest.mark.asyncio
async def test_configured_limited_tariff_switch_reset_completes_grace() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    reset_at = now - timedelta(days=10)
    clock = MutableClock(now)
    billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='LIMITED',
        last_traffic_reset_at=reset_at,
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
        policy=make_policy(reset_traffic_on_tariff_switch=True),
    )
    started = await service.start_if_eligible(billing, GraceReason.LIMITED)
    assert started.session is not None
    original_deadline = started.session.grace_until
    panel.snapshot = replace(
        panel.snapshot,
        used_traffic_bytes=10 * GIB + GIB // 4,
    )
    switched = replace(
        billing,
        tariff_id=2,
        traffic_limit_bytes=5 * GIB,
        used_traffic_bytes=0,
        squad_uuids=(NEW_TARIFF_SQUAD,),
        external_squad_uuid=NEW_EXTERNAL_SQUAD,
    )
    billing_gateway.state = switched

    action = await service.apply_tariff_switch_traffic_reset(billing.subscription_id)

    assert action == GraceCompletionReason.PAID.value
    assert panel.traffic_reset_effects == 1
    assert billing_gateway.state.status == 'active'
    completed = store.only_session()
    assert completed.state is GraceSessionState.COMPLETED
    assert completed.completion_reason is GraceCompletionReason.PAID
    assert completed.grace_until == original_deadline
    assert completed.traffic_reset_target == switched
    assert completed.traffic_reset_remaining_bytes == 3 * GIB // 4
    assert panel.snapshot.used_traffic_bytes == 0
    assert panel.snapshot.traffic_limit_bytes == switched.traffic_limit_bytes
    assert panel.snapshot.squad_uuids == switched.squad_uuids
    assert panel.snapshot.external_squad_uuid == switched.external_squad_uuid

    fence_echo = {
        'id': completed.remnawave_id,
        'status': 'ACTIVE',
        'updatedAt': completed.updated_at.isoformat(),
        'expireAt': completed.overlay.expire_at.isoformat(),
        'trafficLimitBytes': completed.traffic_reset_remaining_bytes,
        'trafficLimitStrategy': completed.overlay.traffic_limit_strategy,
        'activeInternalSquads': [{'uuid': squad_uuid} for squad_uuid in completed.overlay.squad_uuids],
        'externalSquadUuid': completed.overlay.external_squad_uuid,
        'lastTrafficResetAt': completed.traffic_reset_previous_generation,
        'hwidDeviceLimit': completed.billing_before.device_limit,
        'userTraffic': {'usedTrafficBytes': completed.traffic_reset_previous_used_bytes},
    }
    assert (
        await service.should_suppress_webhook(
            billing.subscription_id,
            'user.modified',
            fence_echo,
        )
        is True
    )
    canonical_echo = {
        **fence_echo,
        'expireAt': switched.end_at.isoformat(),
        'trafficLimitBytes': switched.traffic_limit_bytes,
        'activeInternalSquads': [{'uuid': squad_uuid} for squad_uuid in switched.squad_uuids],
        'externalSquadUuid': switched.external_squad_uuid,
    }
    assert (
        await service.should_suppress_webhook(
            billing.subscription_id,
            'user.modified',
            canonical_echo,
        )
        is False
    )


@pytest.mark.asyncio
async def test_deleted_billing_after_reset_effect_is_revoked_from_checkpoint() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    reset_at = now - timedelta(days=10)
    billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='LIMITED',
        last_traffic_reset_at=reset_at,
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=MutableClock(now),
        policy=make_policy(reset_traffic_on_tariff_switch=True),
    )
    started = await service.start_if_eligible(billing, GraceReason.LIMITED)
    assert started.session is not None
    switched = replace(
        billing,
        tariff_id=2,
        traffic_limit_bytes=5 * GIB,
        used_traffic_bytes=0,
        squad_uuids=(NEW_TARIFF_SQUAD,),
    )
    billing_gateway.state = switched
    # Public entry read, pre-reset verification, then deletion after the effect.
    billing_gateway.queued_states = [switched, switched, None]

    action = await service.apply_tariff_switch_traffic_reset(billing.subscription_id)

    assert action == GraceCompletionReason.REVOKED.value
    assert panel.traffic_reset_effects == 1
    assert panel.missing_billing_revocations
    assert panel.snapshot.status == 'DISABLED'
    completed = store.only_session()
    assert completed.state is GraceSessionState.COMPLETED
    assert completed.completion_reason is GraceCompletionReason.REVOKED
    assert completed.traffic_reset_target == switched
    assert completed.traffic_reset_started_at is not None
    enabled_echo = {
        'id': completed.remnawave_id,
        'status': 'ACTIVE',
        'updatedAt': completed.traffic_reset_started_at.isoformat(),
        'expireAt': completed.overlay.expire_at.isoformat(),
        'trafficLimitBytes': completed.traffic_reset_remaining_bytes,
        'trafficLimitStrategy': completed.overlay.traffic_limit_strategy,
        'activeInternalSquads': [{'uuid': squad_uuid} for squad_uuid in completed.overlay.squad_uuids],
        'externalSquadUuid': completed.overlay.external_squad_uuid,
        'lastTrafficResetAt': completed.traffic_reset_previous_generation,
        'hwidDeviceLimit': completed.traffic_reset_target.device_limit,
        'userTraffic': {'usedTrafficBytes': completed.traffic_reset_previous_used_bytes},
    }
    assert (
        await service.should_suppress_webhook(
            billing.subscription_id,
            'user.enabled',
            enabled_echo,
        )
        is True
    )
    reset_echo = {
        **enabled_echo,
        'lastTrafficResetAt': completed.traffic_reset_result_generation,
        'userTraffic': {'usedTrafficBytes': 0},
    }
    assert (
        await service.should_suppress_webhook(
            billing.subscription_id,
            'user.traffic_reset',
            reset_echo,
        )
        is True
    )


@pytest.mark.asyncio
async def test_changed_billing_after_reset_checkpoint_is_applied_before_terminal_conflict() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='LIMITED',
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
        policy=make_policy(reset_traffic_on_tariff_switch=True),
    )
    started = await service.start_if_eligible(billing, GraceReason.LIMITED)
    assert started.session is not None

    first_target = replace(
        billing,
        tariff_id=2,
        tariff_id_known=True,
        traffic_limit_bytes=5 * GIB,
        used_traffic_bytes=0,
        squad_uuids=(NEW_TARIFF_SQUAD,),
    )
    checkpoint = await store.save(
        replace(
            store.only_session(),
            traffic_reset_target=first_target,
            traffic_reset_remaining_bytes=GIB,
        )
    )
    latest = replace(
        first_target,
        tariff_id=3,
        traffic_limit_bytes=7 * GIB,
        squad_uuids=(REGULAR_SQUAD,),
    )
    billing_gateway.state = latest

    result = await service.reconcile()

    assert result.conflicts == 1
    assert panel.applied_billing == [latest]
    assert panel.applied_billing_overlays == [checkpoint.overlay]
    completed = store.only_session()
    assert completed.state is GraceSessionState.COMPLETED
    assert completed.completion_reason is GraceCompletionReason.CONFLICT
    assert completed.traffic_reset_target == first_target
    assert completed.traffic_reset_remaining_bytes == GIB


@pytest.mark.asyncio
async def test_reconciler_infers_configured_tariff_reset_before_service_bridge() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='LIMITED',
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
        policy=make_policy(reset_traffic_on_tariff_switch=True),
    )
    await service.start_if_eligible(billing, GraceReason.LIMITED)
    billing_gateway.state = replace(
        billing,
        tariff_id=2,
        traffic_limit_bytes=5 * GIB,
        used_traffic_bytes=0,
    )

    result = await service.reconcile()

    assert result.paid == 1
    assert result.conflicts == 0
    assert panel.traffic_reset_effects == 1
    assert store.only_session().completion_reason is GraceCompletionReason.PAID


@pytest.mark.asyncio
async def test_used_drop_is_not_tariff_reset_when_policy_is_disabled() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='LIMITED',
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    await service.start_if_eligible(billing, GraceReason.LIMITED)
    billing_gateway.state = replace(
        billing,
        tariff_id=2,
        traffic_limit_bytes=5 * GIB,
        used_traffic_bytes=0,
    )

    result = await service.reconcile()

    assert result.conflicts == 1
    assert panel.traffic_reset_calls == []
    assert store.only_session().completion_reason is GraceCompletionReason.CONFLICT


@pytest.mark.asyncio
@pytest.mark.parametrize('new_limit', [0, 20 * GIB])
async def test_limited_tariff_switch_that_restores_access_completes_grace(
    new_limit: int,
) -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='LIMITED',
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    await service.start_if_eligible(billing, GraceReason.LIMITED)
    billing_gateway.state = replace(
        billing,
        tariff_id=2,
        traffic_limit_bytes=new_limit,
    )

    result = await service.reconcile()

    assert result.paid == 1
    assert result.repaired == 0
    completed = store.only_session()
    assert completed.state is GraceSessionState.COMPLETED
    assert completed.completion_reason is GraceCompletionReason.PAID
    # The second proof is required after the durable webhook-marker checkpoint.
    assert len(panel.prepared_tariff_rebases) == 2
    assert len(panel.applied_billing) == 1
    assert panel.applied_billing[0].status == 'active'
    assert panel.applied_billing[0].traffic_limit_bytes == new_limit
    assert panel.restored_snapshots == []


@pytest.mark.asyncio
async def test_limited_tariff_recovery_allows_its_enabled_webhook_while_panel_transition_is_pending() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='LIMITED',
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    await service.start_if_eligible(billing, GraceReason.LIMITED)
    billing_gateway.state = replace(
        billing,
        tariff_id=2,
        traffic_limit_bytes=20 * GIB,
    )
    panel.pending_billing_attempts = 1

    pending = await service.reconcile()

    assert pending.unchanged == 1
    assert store.only_session().allow_recovery_enabled_webhook is True
    assert (
        await service.should_suppress_webhook(
            billing.subscription_id,
            'user.enabled',
            {},
        )
        is False
    )

    completed = await service.reconcile()
    assert completed.paid == 1
    assert store.only_session().completion_reason is GraceCompletionReason.PAID


@pytest.mark.asyncio
async def test_limited_tariff_recovery_rereads_revocation_after_durable_checkpoint() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='LIMITED',
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    await service.start_if_eligible(billing, GraceReason.LIMITED)
    switched = replace(billing, tariff_id=2, traffic_limit_bytes=20 * GIB)
    revoked = replace(switched, status='disabled')
    billing_gateway.state = switched
    original_save = store.save
    checkpoint_seen = False

    async def save_with_revocation(session: GraceAccessSession) -> GraceAccessSession:
        nonlocal checkpoint_seen
        saved = await original_save(session)
        if session.allow_recovery_enabled_webhook and not checkpoint_seen:
            checkpoint_seen = True
            billing_gateway.state = revoked
        return saved

    store.save = save_with_revocation  # type: ignore[method-assign]

    result = await service.reconcile()

    assert checkpoint_seen is True
    assert result.revoked == 1
    assert result.paid == 0
    assert store.only_session().completion_reason is GraceCompletionReason.REVOKED
    assert panel.applied_billing == [revoked]
    assert all(state.status != 'active' for state in panel.applied_billing)


@pytest.mark.asyncio
async def test_completed_limited_lineage_blocks_tariff_switch_but_allows_later_traffic_purchase() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    reset_at = now - timedelta(days=10)
    clock = MutableClock(now)
    billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='LIMITED',
        last_traffic_reset_at=reset_at,
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    started = await service.start_if_eligible(billing, GraceReason.LIMITED)
    assert started.session is not None
    original_id = started.session.id

    clock.advance(timedelta(days=3, seconds=1))
    assert (await service.reconcile()).timed_out == 1
    completed = store.only_session()
    assert (
        tariff_rebase_lineage_blocks_new_grant(
            replace(billing, traffic_limit_bytes=0),
            completed,
        )
        is False
    )
    assert (
        tariff_rebase_lineage_blocks_new_grant(
            billing,
            replace(
                completed,
                limited_lineage_tail=replace(billing, traffic_limit_bytes=0),
            ),
        )
        is True
    )

    switched = replace(
        billing,
        tariff_id=2,
        traffic_limit_bytes=5 * GIB,
        squad_uuids=(NEW_TARIFF_SQUAD,),
    )
    billing_gateway.state = switched
    panel.snapshot = replace(
        panel.snapshot,
        traffic_limit_bytes=switched.traffic_limit_bytes,
        squad_uuids=switched.squad_uuids,
    )
    blocked = await service.start_if_eligible(switched, GraceReason.LIMITED)

    assert blocked.decision is GraceStartDecision.ALREADY_GRANTED
    assert blocked.session is not None
    assert blocked.session.id == original_id
    assert blocked.session.limited_lineage_tail == switched
    switched_key = build_incident_key(
        switched,
        GraceReason.LIMITED,
        last_traffic_reset_at=reset_at,
    )
    assert switched_key in blocked.session.incident_aliases

    # Reuse the original tariff's old 10 GiB value deliberately: the approved
    # purchase must not collide with the first session's tariff-agnostic key.
    purchased = replace(switched, traffic_limit_bytes=10 * GIB)
    billing_gateway.state = purchased
    panel.snapshot = replace(panel.snapshot, traffic_limit_bytes=purchased.traffic_limit_bytes)
    next_grant = await service.start_if_eligible(purchased, GraceReason.LIMITED)

    assert next_grant.decision is GraceStartDecision.STARTED
    assert next_grant.session is not None
    assert next_grant.session.id != original_id
    assert next_grant.session.limited_lineage_tail == purchased
    assert next_grant.session.incident_key != build_incident_key(
        purchased,
        GraceReason.LIMITED,
        last_traffic_reset_at=reset_at,
    )

    repeated = await service.start_if_eligible(purchased, GraceReason.LIMITED)
    assert repeated.decision is GraceStartDecision.ALREADY_ACTIVE
    assert repeated.session is not None and repeated.session.id == next_grant.session.id

    clock.advance(timedelta(days=3, seconds=1))
    assert (await service.reconcile()).timed_out == 1
    repeated_after_completion = await service.start_if_eligible(
        purchased,
        GraceReason.LIMITED,
    )
    assert repeated_after_completion.decision is GraceStartDecision.ALREADY_GRANTED
    assert repeated_after_completion.session is not None
    assert repeated_after_completion.session.id == next_grant.session.id


@pytest.mark.asyncio
async def test_expired_tariff_switch_rebases_active_grace_at_same_expiry() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = make_snapshot(expire_at=billing.end_at)
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    started = await service.start_if_eligible(billing, GraceReason.EXPIRED)
    assert started.session is not None
    original = started.session

    changed_billing = replace(
        billing,
        tariff_id=2,
        traffic_limit_bytes=5 * GIB,
        device_limit=4,
        squad_uuids=(NEW_TARIFF_SQUAD,),
        external_squad_uuid=NEW_EXTERNAL_SQUAD,
    )
    billing_gateway.state = changed_billing

    result = await service.reconcile()

    assert result.repaired == 1
    assert result.conflicts == 0
    rebased = store.only_session()
    assert rebased.id == original.id
    assert rebased.overlay == original.overlay
    assert rebased.grace_until == original.grace_until
    assert rebased.panel_before.status == snapshot.status
    assert rebased.panel_before.expire_at == snapshot.expire_at
    assert rebased.panel_before.traffic_limit_bytes == changed_billing.traffic_limit_bytes
    assert rebased.panel_before.squad_uuids == changed_billing.squad_uuids
    assert len(panel.applied_overlays) == 1
    assert panel.applied_billing == []

    clock.advance(timedelta(days=3, seconds=1))
    timed_out = await service.reconcile()

    assert timed_out.timed_out == 1
    assert panel.restored_snapshots[-1][1] == rebased.panel_before
    repeated = await service.start_if_eligible(changed_billing, GraceReason.EXPIRED)
    assert repeated.decision is GraceStartDecision.ALREADY_GRANTED


@pytest.mark.asyncio
async def test_configured_expired_tariff_reset_preserves_remaining_grace() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    reset_at = now - timedelta(days=10)
    clock = MutableClock(now)
    billing = replace(
        make_billing(
            status='expired',
            end_at=now - timedelta(days=1),
            traffic_limit_bytes=10 * GIB,
            used_traffic_bytes=10 * GIB,
        ),
        traffic_limit_strategy='MONTH',
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='EXPIRED',
        last_traffic_reset_at=reset_at,
        traffic_limit_strategy=billing.traffic_limit_strategy,
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
        policy=make_policy(reset_traffic_on_tariff_switch=True),
    )
    started = await service.start_if_eligible(billing, GraceReason.EXPIRED)
    assert started.session is not None
    original = started.session
    panel.snapshot = replace(
        panel.snapshot,
        used_traffic_bytes=10 * GIB + GIB // 4,
    )
    switched = replace(
        billing,
        tariff_id=2,
        traffic_limit_bytes=5 * GIB,
        used_traffic_bytes=0,
        squad_uuids=(NEW_TARIFF_SQUAD,),
        external_squad_uuid=NEW_EXTERNAL_SQUAD,
        traffic_limit_strategy='DAY',
    )
    billing_gateway.state = switched

    result = await service.reconcile()

    assert result.repaired == 1
    assert result.errors == 0
    assert panel.traffic_reset_effects == 1
    continued = store.only_session()
    assert continued.state is GraceSessionState.ACTIVE
    assert continued.id == original.id
    assert continued.started_at == original.started_at
    assert continued.grace_until == original.grace_until
    assert continued.overlay.expire_at == original.overlay.expire_at
    assert continued.overlay.squad_uuids == original.overlay.squad_uuids
    assert continued.overlay.external_squad_uuid is None
    assert continued.overlay.traffic_limit_bytes == 3 * GIB // 4
    assert continued.billing_before == switched
    assert continued.panel_before.traffic_limit_bytes == switched.traffic_limit_bytes
    assert continued.panel_before.squad_uuids == switched.squad_uuids
    assert continued.panel_before.traffic_limit_strategy == switched.traffic_limit_strategy
    assert continued.panel_before.last_traffic_reset_at != reset_at
    assert continued.overlay.expected_last_traffic_reset_at == continued.panel_before.last_traffic_reset_at
    assert continued.traffic_reset_target is None
    assert continued.traffic_reset_remaining_bytes == 3 * GIB // 4

    stable = await service.reconcile()

    assert stable.unchanged == 1
    assert panel.external_reset_revocations == []

    clock.advance(timedelta(days=3, seconds=1))
    timed_out = await service.reconcile()

    assert timed_out.timed_out == 1
    assert panel.restored_snapshots[-1][1] == continued.panel_before


@pytest.mark.asyncio
async def test_continued_reset_fence_echo_is_suppressed_after_later_payment() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(
        status='expired',
        end_at=now - timedelta(days=1),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='EXPIRED',
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
        policy=make_policy(reset_traffic_on_tariff_switch=True),
    )
    await service.start_if_eligible(billing, GraceReason.EXPIRED)
    panel.snapshot = replace(
        panel.snapshot,
        used_traffic_bytes=10 * GIB + GIB // 4,
    )
    switched = replace(
        billing,
        tariff_id=2,
        traffic_limit_bytes=5 * GIB,
        used_traffic_bytes=0,
        squad_uuids=(NEW_TARIFF_SQUAD,),
    )
    billing_gateway.state = switched
    continued_result = await service.reconcile()
    assert continued_result.repaired == 1
    continued = store.only_session()
    assert continued.traffic_reset_target is None
    assert continued.traffic_reset_remaining_bytes == 3 * GIB // 4
    assert continued.traffic_reset_started_at is not None
    fence_updated_at = continued.traffic_reset_started_at

    clock.advance(timedelta(minutes=10))
    billing_gateway.state = replace(
        switched,
        status='active',
        end_at=now + timedelta(days=30),
    )
    paid_result = await service.reconcile()

    assert paid_result.paid == 1
    completed = store.only_session()
    assert completed.state is GraceSessionState.COMPLETED
    assert completed.traffic_reset_target is None
    assert completed.traffic_reset_remaining_bytes == 3 * GIB // 4
    fence_echo = {
        'id': completed.remnawave_id,
        'status': 'ACTIVE',
        'updatedAt': fence_updated_at.isoformat(),
        'expireAt': completed.overlay.expire_at.isoformat(),
        'trafficLimitBytes': completed.overlay.traffic_limit_bytes,
        'trafficLimitStrategy': completed.overlay.traffic_limit_strategy,
        'activeInternalSquads': [{'uuid': squad_uuid} for squad_uuid in completed.overlay.squad_uuids],
        'externalSquadUuid': completed.overlay.external_squad_uuid,
        'lastTrafficResetAt': completed.traffic_reset_previous_generation,
        'hwidDeviceLimit': completed.billing_before.device_limit,
        'userTraffic': {'usedTrafficBytes': completed.traffic_reset_previous_used_bytes},
    }
    assert (
        await service.should_suppress_webhook(
            billing.subscription_id,
            'user.modified',
            fence_echo,
        )
        is True
    )
    reset_echo = {
        **fence_echo,
        'lastTrafficResetAt': completed.traffic_reset_result_generation,
        'userTraffic': {'usedTrafficBytes': 0},
    }
    assert (
        await service.should_suppress_webhook(
            billing.subscription_id,
            'user.enabled',
            fence_echo,
        )
        is True
    )
    assert await service.should_suppress_webhook(billing.subscription_id, 'user.enabled', reset_echo) is False
    assert (
        await service.should_suppress_webhook(
            billing.subscription_id,
            'user.traffic_reset',
            reset_echo,
        )
        is True
    )
    sparse_reset_echo = {
        'id': completed.remnawave_id,
        'updatedAt': fence_updated_at.isoformat(),
    }
    assert (
        await service.should_suppress_webhook(
            billing.subscription_id,
            'user.enabled',
            sparse_reset_echo,
        )
        is False
    )
    assert (
        await service.should_suppress_webhook(
            billing.subscription_id,
            'user.traffic_reset',
            sparse_reset_echo,
        )
        is False
    )
    assert (
        await service.should_suppress_webhook(
            billing.subscription_id,
            'user.enabled',
            {},
        )
        is False
    )
    unrelated_reset_echo = {
        **sparse_reset_echo,
        'id': OTHER_PANEL_ID,
    }
    assert (
        await service.should_suppress_webhook(
            billing.subscription_id,
            'user.enabled',
            unrelated_reset_echo,
        )
        is False
    )


@pytest.mark.asyncio
async def test_reset_finish_checkpoint_is_durable_before_later_billing_read() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='LIMITED',
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=MutableClock(now),
        policy=make_policy(reset_traffic_on_tariff_switch=True),
    )
    await service.start_if_eligible(billing, GraceReason.LIMITED)
    switched = replace(
        billing,
        tariff_id=2,
        traffic_limit_bytes=5 * GIB,
        used_traffic_bytes=0,
    )
    billing_gateway.state = switched
    billing_gateway.fail_on_get_call = billing_gateway.get_calls + 3

    with pytest.raises(RuntimeError, match='lost canonical read'):
        await service.apply_tariff_switch_traffic_reset(billing.subscription_id)

    checkpoint = store.only_session()
    assert checkpoint.state is GraceSessionState.ACTIVE
    assert checkpoint.traffic_reset_target == switched
    assert checkpoint.traffic_reset_started_at is not None
    assert checkpoint.traffic_reset_finished_at is not None
    assert panel.traffic_reset_effects == 1

    billing_gateway.fail_on_get_call = None
    action = await service.apply_tariff_switch_traffic_reset(billing.subscription_id)

    assert action == GraceCompletionReason.PAID.value
    assert panel.traffic_reset_effects == 1


@pytest.mark.asyncio
async def test_expired_tariff_reset_retry_does_not_reset_or_regrant_twice() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(
        status='expired',
        end_at=now - timedelta(days=1),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='EXPIRED',
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
        policy=make_policy(reset_traffic_on_tariff_switch=True),
    )
    await service.start_if_eligible(billing, GraceReason.EXPIRED)
    panel.snapshot = replace(
        panel.snapshot,
        used_traffic_bytes=10 * GIB + GIB // 2,
    )
    billing_gateway.state = replace(
        billing,
        tariff_id=2,
        traffic_limit_bytes=5 * GIB,
        used_traffic_bytes=0,
    )
    panel.fail_traffic_reset_after_effect = 1

    failed = await service.reconcile()

    assert failed.errors == 1
    pending = store.only_session()
    assert pending.traffic_reset_target is not None
    assert pending.traffic_reset_remaining_bytes == GIB // 2
    assert pending.traffic_reset_started_at is not None
    assert pending.traffic_reset_finished_at is None
    assert panel.traffic_reset_effects == 1

    retried = await service.reconcile()

    assert retried.repaired == 1
    assert panel.traffic_reset_effects == 1
    assert store.only_session().traffic_reset_finished_at is not None
    assert store.only_session().overlay.traffic_limit_bytes == GIB // 2


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', [GraceReason.LIMITED, GraceReason.EXPIRED])
async def test_tariff_change_before_restore_uses_fresh_canonical_state(
    reason: GraceReason,
) -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    limited = reason is GraceReason.LIMITED
    billing = make_billing(
        status=reason.value,
        end_at=now + timedelta(days=20) if limited else now - timedelta(days=1),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB if limited else 3 * GIB,
    )
    snapshot = make_snapshot(
        expire_at=billing.end_at,
        traffic_limit_bytes=billing.traffic_limit_bytes,
        used_traffic_bytes=billing.used_traffic_bytes,
    )
    if limited:
        snapshot = replace(snapshot, status='LIMITED')
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    await service.start_if_eligible(billing, reason)
    changed = replace(
        billing,
        tariff_id=2,
        traffic_limit_bytes=5 * GIB,
        squad_uuids=(NEW_TARIFF_SQUAD,),
    )
    clock.advance(timedelta(days=3, seconds=1))
    billing_gateway.queued_states = [billing, changed]

    result = await service.reconcile()

    assert result.conflicts == 1
    assert panel.restored_snapshots == []
    assert panel.applied_billing == [changed]
    completed = store.only_session()
    assert completed.completion_reason is GraceCompletionReason.CONFLICT
    if limited:
        assert completed.limited_lineage_tail == changed


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', [GraceReason.LIMITED, GraceReason.EXPIRED])
async def test_tariff_change_during_restore_overwrites_stale_snapshot_with_fresh_canonical_state(
    reason: GraceReason,
) -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    limited = reason is GraceReason.LIMITED
    billing = make_billing(
        status=reason.value,
        end_at=now + timedelta(days=20) if limited else now - timedelta(days=1),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB if limited else 3 * GIB,
    )
    snapshot = make_snapshot(
        expire_at=billing.end_at,
        traffic_limit_bytes=billing.traffic_limit_bytes,
        used_traffic_bytes=billing.used_traffic_bytes,
    )
    if limited:
        snapshot = replace(snapshot, status='LIMITED')
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    await service.start_if_eligible(billing, reason)
    changed = replace(
        billing,
        tariff_id=2,
        traffic_limit_bytes=5 * GIB,
        squad_uuids=(NEW_TARIFF_SQUAD,),
    )
    original_save = store.save
    restoring_metadata_checkpoint_seen = False

    async def observe_restoring_metadata_save(
        session: GraceAccessSession,
    ) -> GraceAccessSession:
        nonlocal restoring_metadata_checkpoint_seen
        if limited and session.state is GraceSessionState.RESTORING and session.limited_lineage_tail == changed:
            restoring_metadata_checkpoint_seen = True
            billing_gateway.state = replace(changed, tariff_id=3)
        return await original_save(session)

    store.save = observe_restoring_metadata_save  # type: ignore[method-assign]
    clock.advance(timedelta(days=3, seconds=1))
    billing_gateway.queued_states = [billing, billing, changed]

    result = await service.reconcile()

    assert result.conflicts == 1
    assert restoring_metadata_checkpoint_seen is False
    assert panel.restored_snapshots == [(PANEL_ID, snapshot)]
    assert panel.applied_billing == [changed]
    completed = store.only_session()
    assert completed.completion_reason is GraceCompletionReason.CONFLICT
    if limited:
        assert completed.limited_lineage_tail == changed


@pytest.mark.asyncio
@pytest.mark.parametrize('timing', ['before_restore', 'during_restore'])
async def test_revocation_during_restore_wins_over_stale_snapshot(timing: str) -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='LIMITED',
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    await service.start_if_eligible(billing, GraceReason.LIMITED)
    revoked = replace(billing, status='disabled')
    clock.advance(timedelta(days=3, seconds=1))
    billing_gateway.queued_states = [billing, revoked] if timing == 'before_restore' else [billing, billing, revoked]

    result = await service.reconcile()

    assert result.revoked == 1
    assert panel.applied_billing == [revoked]
    expected_restores = [] if timing == 'before_restore' else [(PANEL_ID, snapshot)]
    assert panel.restored_snapshots == expected_restores
    assert store.only_session().completion_reason is GraceCompletionReason.REVOKED


@pytest.mark.asyncio
async def test_multiple_tariff_switches_keep_one_grace_grant_and_deadline() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='LIMITED',
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    started = await service.start_if_eligible(billing, GraceReason.LIMITED)
    assert started.session is not None
    original = started.session

    traversed: list[GraceBillingState] = []
    for tariff_id, limit, squads in (
        (2, 8 * GIB, (NEW_TARIFF_SQUAD,)),
        (3, 6 * GIB, (REGULAR_SQUAD, NEW_TARIFF_SQUAD)),
        (1, 10 * GIB, (REGULAR_SQUAD,)),
    ):
        billing_gateway.state = replace(
            billing_gateway.state,
            tariff_id=tariff_id,
            traffic_limit_bytes=limit,
            squad_uuids=squads,
        )
        traversed.append(billing_gateway.state)
        result = await service.reconcile()
        assert result.repaired == 1
        current = store.only_session()
        assert current.id == original.id
        assert current.incident_key == original.incident_key
        assert current.grace_until == original.grace_until
        assert current.overlay == original.overlay

    assert len(panel.applied_overlays) == 1
    assert panel.applied_billing == []
    assert store.only_session().billing_before.tariff_id == 1

    clock.advance(timedelta(days=3, seconds=1))
    assert (await service.reconcile()).timed_out == 1
    for traversed_billing in traversed:
        billing_gateway.state = traversed_billing
        panel.snapshot = replace(
            panel.snapshot,
            status='LIMITED',
            expire_at=traversed_billing.end_at,
            traffic_limit_bytes=traversed_billing.traffic_limit_bytes,
            squad_uuids=traversed_billing.squad_uuids,
        )
        repeated = await service.start_if_eligible(
            traversed_billing,
            GraceReason.LIMITED,
        )
        assert repeated.decision is GraceStartDecision.ALREADY_GRANTED
        assert repeated.session is not None
        assert repeated.session.id == original.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('billing_change', 'panel_change'),
    [
        ({'tariff_id': 2, 'used_traffic_bytes': 0}, {}),
        ({'tariff_id': 2, 'end_at_delta': timedelta(minutes=-1)}, {}),
        ({'tariff_id': 2}, {'status': 'DISABLED'}),
        ({'tariff_id': 2}, {'squad_uuids': (NEW_TARIFF_SQUAD,)}),
        ({'tariff_id': None, 'tariff_id_known': False, 'traffic_limit_bytes': 5 * GIB}, {}),
    ],
)
async def test_unproven_tariff_change_remains_fail_closed(
    billing_change: dict[str, object],
    panel_change: dict[str, object],
) -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='LIMITED',
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    await service.start_if_eligible(billing, GraceReason.LIMITED)

    changes = dict(billing_change)
    end_delta = changes.pop('end_at_delta', None)
    if end_delta is not None:
        changes['end_at'] = billing.end_at + end_delta
    billing_gateway.state = replace(billing, **changes)
    if panel_change:
        panel.snapshot = replace(panel.snapshot, **panel_change)

    result = await service.reconcile()

    assert result.conflicts == 1
    assert store.only_session().completion_reason is GraceCompletionReason.CONFLICT
    assert len(panel.applied_overlays) == 1


@pytest.mark.asyncio
async def test_legacy_session_without_tariff_identity_cannot_rebase_to_known_tariff() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    legacy_billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
        tariff_id=None,
        tariff_id_known=False,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=legacy_billing.end_at,
            traffic_limit_bytes=legacy_billing.traffic_limit_bytes,
            used_traffic_bytes=legacy_billing.used_traffic_bytes,
        ),
        status='LIMITED',
    )
    service, store, panel, billing_gateway = make_service(
        billing=legacy_billing,
        snapshot=snapshot,
        clock=clock,
    )
    started = await service.start_if_eligible(legacy_billing, GraceReason.LIMITED)
    assert started.session is not None
    original_id = started.session.id
    # Simulate a session persisted before tariff lineage metadata existed.
    store.sessions[original_id] = replace(
        started.session,
        incident_aliases=(),
        limited_lineage_tail=None,
    )
    changed = replace(
        legacy_billing,
        tariff_id=2,
        tariff_id_known=True,
        traffic_limit_bytes=5 * GIB,
    )
    billing_gateway.state = changed

    result = await service.reconcile()

    assert result.conflicts == 1
    assert store.only_session().completion_reason is GraceCompletionReason.CONFLICT
    assert len(panel.applied_billing) == 1

    # A subsequent worker pass must derive the old lineage from immutable
    # snapshots and must not mint a second Grace grant after the conflict.
    panel.snapshot = replace(
        snapshot,
        status='LIMITED',
        traffic_limit_bytes=changed.traffic_limit_bytes,
        used_traffic_bytes=changed.used_traffic_bytes,
        squad_uuids=changed.squad_uuids,
        external_squad_uuid=changed.external_squad_uuid,
    )
    repeated = await service.start_if_eligible(changed, GraceReason.LIMITED)
    assert repeated.decision is GraceStartDecision.ALREADY_GRANTED
    assert repeated.session is not None and repeated.session.id == original_id
    assert repeated.session.limited_lineage_tail == changed
    assert len(panel.applied_overlays) == 1

    # The conservative first block seeds a known tail, so a later real quota
    # purchase on that same tariff is no longer permanently false-blocked.
    purchased = replace(changed, traffic_limit_bytes=7 * GIB)
    billing_gateway.state = purchased
    panel.snapshot = replace(panel.snapshot, traffic_limit_bytes=purchased.traffic_limit_bytes)
    next_grant = await service.start_if_eligible(purchased, GraceReason.LIMITED)
    assert next_grant.decision is GraceStartDecision.STARTED
    assert next_grant.session is not None and next_grant.session.id != original_id


@pytest.mark.asyncio
async def test_paid_recovery_still_wins_after_tariff_rebase() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='LIMITED',
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    await service.start_if_eligible(billing, GraceReason.LIMITED)
    switched = replace(billing, tariff_id=2, traffic_limit_bytes=5 * GIB)
    billing_gateway.state = switched
    assert (await service.reconcile()).repaired == 1

    recovered = replace(
        switched,
        status='active',
        end_at=switched.end_at + timedelta(days=30),
        used_traffic_bytes=0,
    )
    billing_gateway.state = recovered
    result = await service.reconcile()

    assert result.paid == 1
    assert store.only_session().completion_reason is GraceCompletionReason.PAID
    assert panel.applied_billing == [recovered]
    assert panel.restored_snapshots == []


@pytest.mark.asyncio
async def test_expired_paid_recovery_still_wins_after_tariff_rebase() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = make_snapshot(expire_at=billing.end_at)
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    await service.start_if_eligible(billing, GraceReason.EXPIRED)
    switched = replace(
        billing,
        tariff_id=2,
        traffic_limit_bytes=5 * GIB,
        squad_uuids=(NEW_TARIFF_SQUAD,),
    )
    billing_gateway.state = switched
    assert (await service.reconcile()).repaired == 1

    recovered = replace(
        switched,
        status='active',
        end_at=now + timedelta(days=30),
        used_traffic_bytes=0,
    )
    billing_gateway.state = recovered
    result = await service.reconcile()

    assert result.paid == 1
    assert store.only_session().completion_reason is GraceCompletionReason.PAID
    assert panel.applied_billing == [recovered]
    assert panel.restored_snapshots == []


@pytest.mark.asyncio
async def test_canonical_squad_change_ends_grace_and_applies_fresh_billing() -> None:
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = make_snapshot(expire_at=billing.end_at)
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    await service.start_if_eligible(billing, GraceReason.EXPIRED)

    changed_billing = replace(
        billing,
        squad_uuids=('55555555-5555-5555-5555-555555555555',),
    )
    billing_gateway.state = changed_billing

    result = await service.reconcile()

    assert result.conflicts == 1
    assert panel.applied_billing == [changed_billing]
    completed = next(iter(store.sessions.values()))
    assert completed.state is GraceSessionState.COMPLETED
    assert completed.completion_reason is GraceCompletionReason.CONFLICT


@pytest.mark.asyncio
@pytest.mark.parametrize('relinked_panel_id', [OTHER_PANEL_ID, None])
async def test_panel_identity_change_restores_the_old_user_instead_of_pushing_billing_onto_it(
    relinked_panel_id: int | None,
) -> None:
    # Подписку перелинковали на другого панельного юзера (или связь потеряли).
    # Канонический биллинг описывает уже не тот id, под которым выдан оверлей,
    # поэтому применять его к старому пользователю нельзя — только откат
    # снапшота на прежний id. `None` не должен «совпасть» с id сессии.
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = make_snapshot(expire_at=billing.end_at)
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    await service.start_if_eligible(billing, GraceReason.EXPIRED)

    billing_gateway.state = replace(billing, remnawave_id=relinked_panel_id)

    result = await service.reconcile()

    assert result.conflicts == 1
    assert panel.applied_billing == []
    assert panel.restored_snapshots == [(PANEL_ID, snapshot)]
    completed = store.only_session()
    assert completed.state is GraceSessionState.COMPLETED
    assert completed.completion_reason is GraceCompletionReason.CONFLICT


@pytest.mark.asyncio
async def test_limited_canonical_change_waits_without_error_then_completes() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='LIMITED',
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    started = await service.start_if_eligible(billing, GraceReason.LIMITED)
    assert started.session is not None

    changed_billing = replace(
        billing,
        traffic_limit_bytes=20 * GIB,
        squad_uuids=('55555555-5555-5555-5555-555555555555',),
    )
    billing_gateway.state = changed_billing
    panel.pending_billing_attempts = 1
    store.sessions[started.session.id] = replace(
        store.only_session(),
        last_error='RemnaWaveAPIError: invalid status LIMITED',
    )

    pending = await service.reconcile()

    assert pending.unchanged == 1
    assert pending.errors == 0
    assert pending.conflicts == 0
    assert store.only_session().state is GraceSessionState.ACTIVE
    assert store.only_session().last_error is None
    assert panel.applied_billing_overlays == [started.session.overlay]

    completed = await service.reconcile()

    assert completed.conflicts == 1
    assert completed.errors == 0
    assert store.only_session().state is GraceSessionState.COMPLETED
    assert store.only_session().completion_reason is GraceCompletionReason.CONFLICT
    assert panel.applied_billing == [changed_billing, changed_billing]
    assert panel.applied_billing_overlays == [
        started.session.overlay,
        started.session.overlay,
    ]


@pytest.mark.asyncio
async def test_limited_transition_conflict_keeps_protection_open() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=billing.used_traffic_bytes,
        ),
        status='LIMITED',
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    started = await service.start_if_eligible(billing, GraceReason.LIMITED)
    assert started.session is not None

    changed_billing = replace(billing, traffic_limit_bytes=20 * GIB)
    billing_gateway.state = changed_billing
    panel.conflict_billing_attempts = 1

    result = await service.reconcile()

    assert result.conflicts == 1
    assert result.errors == 0
    restoring = store.only_session()
    assert restoring.state is GraceSessionState.RESTORING
    assert restoring.completion_reason is None
    assert restoring.completed_at is None
    assert restoring.last_error == 'GracePanelTransitionConflict: panel state changed outside grace'
    assert panel.applied_billing == [changed_billing]
    assert panel.applied_billing_overlays == [started.session.overlay]


@pytest.mark.asyncio
async def test_webhook_suppression_matches_only_grace_echo() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = make_snapshot(expire_at=billing.end_at)
    service, _, _, billing_gateway = make_service(billing=billing, snapshot=snapshot, clock=clock)
    started = await service.start_if_eligible(billing, GraceReason.EXPIRED)
    assert started.session is not None
    overlay = started.session.overlay

    grace_echo = {
        'id': started.session.remnawave_id,
        'status': 'ACTIVE',
        'updatedAt': started.session.updated_at.isoformat(),
        'expireAt': overlay.expire_at.isoformat(),
        'trafficLimitBytes': overlay.traffic_limit_bytes,
        'trafficLimitStrategy': overlay.traffic_limit_strategy,
        'activeInternalSquads': [{'uuid': EXPIRED_SQUAD}],
        'externalSquadUuid': overlay.external_squad_uuid,
        'lastTrafficResetAt': overlay.expected_last_traffic_reset_at,
        'hwidDeviceLimit': billing.device_limit,
        'userTraffic': {'usedTrafficBytes': snapshot.used_traffic_bytes},
    }
    real_update = {
        'status': 'ACTIVE',
        'expireAt': (now + timedelta(days=30)).isoformat(),
        'trafficLimitBytes': 20 * GIB,
        'activeInternalSquads': [{'uuid': REGULAR_SQUAD}],
    }

    assert await service.should_suppress_webhook(42, 'user.modified', grace_echo) is True
    assert await service.should_suppress_webhook(42, 'user.modified', real_update) is False
    assert await service.should_suppress_webhook(42, 'user.enabled', {}) is False
    # The final activation PATCH can emit user.enabled. Its complete overlay
    # snapshot and durable mutation window distinguish it from an admin enable.
    assert await service.should_suppress_webhook(42, 'user.enabled', grace_echo) is True
    assert await service.should_suppress_webhook(42, 'user.disabled', grace_echo) is False

    billing_gateway.state = replace(billing, status='active', end_at=now + timedelta(days=30))
    # A delayed echo from the old overlay must still be suppressed until the
    # reconciliation transaction closes the persisted grace session.
    assert await service.should_suppress_webhook(42, 'user.enabled', grace_echo) is True


@pytest.mark.asyncio
async def test_exact_restore_modified_echo_is_suppressed_while_session_is_open() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = replace(make_snapshot(expire_at=billing.end_at), status='EXPIRED')
    service, store, _, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    started = await service.start_if_eligible(billing, GraceReason.EXPIRED)
    assert started.session is not None
    clock.advance(timedelta(days=3, seconds=1))
    restoring = replace(
        started.session,
        state=GraceSessionState.RESTORING,
        restore_started_at=clock(),
        updated_at=clock(),
    )
    await store.save(restoring)

    assert (
        await service.should_suppress_webhook(
            billing.subscription_id,
            'user.modified',
            make_restore_modified_echo(restoring),
        )
        is True
    )


@pytest.mark.asyncio
async def test_exact_restore_modified_echo_is_suppressed_after_timeout_completion() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = replace(make_snapshot(expire_at=billing.end_at), status='EXPIRED')
    service, store, _, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    started = await service.start_if_eligible(billing, GraceReason.EXPIRED)
    assert started.session is not None
    clock.advance(timedelta(days=3, seconds=1))

    reconciled = await service.reconcile()

    assert reconciled.timed_out == 1
    completed = store.only_session()
    assert completed.completion_reason is GraceCompletionReason.TIMEOUT
    assert (
        await service.should_suppress_webhook(
            billing.subscription_id,
            'user.modified',
            make_restore_modified_echo(completed),
        )
        is True
    )


@pytest.mark.asyncio
async def test_force_restore_echo_accepts_disabled_status_after_grace_deadline() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = replace(make_snapshot(expire_at=billing.end_at), status='EXPIRED')
    service, store, _, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    started = await service.start_if_eligible(billing, GraceReason.EXPIRED)
    assert started.session is not None
    clock.advance(timedelta(days=3, seconds=1))
    restoring = replace(
        started.session,
        state=GraceSessionState.RESTORING,
        restore_started_at=clock(),
        restore_force_disable=True,
        updated_at=clock(),
    )
    await store.save(restoring)
    disabled_echo = {
        **make_restore_modified_echo(restoring),
        'status': 'DISABLED',
    }
    disabled_overlay_echo = {
        **disabled_echo,
        'trafficLimitBytes': restoring.overlay.traffic_limit_bytes,
        'trafficLimitStrategy': restoring.overlay.traffic_limit_strategy,
        'activeInternalSquads': [{'uuid': squad_uuid} for squad_uuid in restoring.overlay.squad_uuids],
        'externalSquadUuid': restoring.overlay.external_squad_uuid,
    }

    assert (
        await service.should_suppress_webhook(
            billing.subscription_id,
            'user.modified',
            disabled_echo,
        )
        is True
    )
    assert (
        await service.should_suppress_webhook(
            billing.subscription_id,
            'user.modified',
            disabled_overlay_echo,
        )
        is True
    )


@pytest.mark.asyncio
async def test_restore_echo_accepts_future_expiry_from_disabled_snapshot() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = make_snapshot(expire_at=now + timedelta(days=10))
    service, store, _, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    started = await service.start_if_eligible(billing, GraceReason.EXPIRED)
    assert started.session is not None

    drained = await service.drain(force_restore=True)

    assert drained.drained == 1
    completed = store.only_session()
    canonical_echo = {
        **make_restore_modified_echo(completed),
        'status': 'DISABLED',
        'expireAt': snapshot.expire_at.isoformat(),
    }
    assert (
        await service.should_suppress_webhook(
            billing.subscription_id,
            'user.modified',
            canonical_echo,
        )
        is True
    )


@pytest.mark.asyncio
async def test_partial_modified_payload_is_not_accepted_as_a_restore_echo() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = replace(make_snapshot(expire_at=billing.end_at), status='EXPIRED')
    service, store, _, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    started = await service.start_if_eligible(billing, GraceReason.EXPIRED)
    assert started.session is not None
    clock.advance(timedelta(days=3, seconds=1))
    await service.reconcile()
    completed = store.only_session()

    assert (
        await service.should_suppress_webhook(
            billing.subscription_id,
            'user.modified',
            {
                'id': completed.remnawave_id,
                'expireAt': completed.overlay.expire_at.isoformat(),
            },
        )
        is False
    )


@pytest.mark.asyncio
async def test_delayed_restore_echo_remains_provable_without_weakening_incident_dedupe() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = replace(make_snapshot(expire_at=billing.end_at), status='EXPIRED')
    service, store, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    started = await service.start_if_eligible(billing, GraceReason.EXPIRED)
    assert started.session is not None
    clock.advance(timedelta(days=3, seconds=1))
    await service.reconcile()
    completed = store.only_session()
    restore_echo = make_restore_modified_echo(completed)
    clock.advance(timedelta(minutes=16))

    assert (
        await service.should_suppress_webhook(
            billing.subscription_id,
            'user.modified',
            restore_echo,
        )
        is True
    )

    echoed_billing = replace(billing, end_at=completed.overlay.expire_at)
    panel.snapshot = replace(panel.snapshot, expire_at=completed.overlay.expire_at)
    duplicate = await service.start_if_eligible(echoed_billing, GraceReason.EXPIRED)
    assert duplicate.decision is GraceStartDecision.ALREADY_GRANTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'changed_fields',
    [
        {'id': 9999},
        {'status': 'DISABLED'},
        {'expireAt': '2026-07-18T13:01:00+00:00'},
        {'trafficLimitBytes': 101 * GIB},
        {'activeInternalSquads': [{'uuid': LIMITED_SQUAD}]},
        {'externalSquadUuid': LIMITED_SQUAD},
        {'updatedAt': '2026-07-18T12:05:01+00:00'},
    ],
)
async def test_mismatching_manual_modified_event_is_not_suppressed_by_completed_session(
    changed_fields: dict[str, object],
) -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = replace(make_snapshot(expire_at=billing.end_at), status='EXPIRED')
    service, store, _, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    started = await service.start_if_eligible(billing, GraceReason.EXPIRED)
    assert started.session is not None
    clock.advance(timedelta(days=3, seconds=1))
    await service.reconcile()
    completed = store.only_session()
    restore_echo = make_restore_modified_echo(completed)
    manual_update = {**restore_echo, **changed_fields}

    assert (
        await service.should_suppress_webhook(
            billing.subscription_id,
            'user.modified',
            manual_update,
        )
        is False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'completion_reason',
    [GraceCompletionReason.TIMEOUT, GraceCompletionReason.DRAINED],
)
async def test_completed_expired_grace_deduplicates_overlay_expiry_but_allows_new_term(
    completion_reason: GraceCompletionReason,
) -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = replace(make_snapshot(expire_at=billing.end_at), status='EXPIRED')
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    started = await service.start_if_eligible(billing, GraceReason.EXPIRED)
    assert started.session is not None

    if completion_reason is GraceCompletionReason.TIMEOUT:
        clock.advance(timedelta(days=3, seconds=1))
        completed_result = await service.reconcile()
        assert completed_result.timed_out == 1
    else:
        completed_result = await service.drain(force_restore=True)
        assert completed_result.drained == 1

    completed = store.only_session()
    assert completed.completion_reason is completion_reason
    assert panel.restore_force_flags == [completion_reason is GraceCompletionReason.DRAINED]
    overlay_apply_count = len(panel.applied_overlays)
    overlay_expiry = replace(billing, end_at=completed.overlay.expire_at)
    panel.snapshot = replace(
        panel.snapshot,
        status='EXPIRED',
        expire_at=completed.overlay.expire_at,
    )

    assert (
        await service.should_suppress_webhook(
            billing.subscription_id,
            'user.modified',
            make_restore_modified_echo(completed),
        )
        is True
    )
    disabled_overlay_echo = {
        **make_restore_modified_echo(completed),
        'status': 'DISABLED',
        'trafficLimitBytes': completed.overlay.traffic_limit_bytes,
        'trafficLimitStrategy': completed.overlay.traffic_limit_strategy,
        'activeInternalSquads': [{'uuid': squad_uuid} for squad_uuid in completed.overlay.squad_uuids],
        'externalSquadUuid': completed.overlay.external_squad_uuid,
    }
    assert await service.should_suppress_webhook(
        billing.subscription_id,
        'user.modified',
        disabled_overlay_echo,
    ) is (completion_reason is GraceCompletionReason.DRAINED)

    duplicate = await service.start_if_eligible(overlay_expiry, GraceReason.EXPIRED)

    assert duplicate.decision is GraceStartDecision.ALREADY_GRANTED
    assert duplicate.session == completed
    assert len(store.sessions) == 1
    assert len(panel.applied_overlays) == overlay_apply_count

    new_end_at = completed.overlay.expire_at - timedelta(hours=1)
    new_incident = replace(billing, end_at=new_end_at)
    billing_gateway.state = new_incident
    panel.snapshot = replace(panel.snapshot, expire_at=new_end_at)

    restarted = await service.start_if_eligible(new_incident, GraceReason.EXPIRED)

    assert restarted.decision is GraceStartDecision.STARTED
    assert restarted.session is not None
    assert restarted.session.id != completed.id
    assert restarted.session.incident_key == build_incident_key(new_incident, GraceReason.EXPIRED)
    assert len(store.sessions) == 2
    assert len(panel.applied_overlays) == overlay_apply_count + 1


@pytest.mark.asyncio
async def test_completed_expired_echo_dedupe_does_not_hide_changed_canonical_tariff() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = replace(make_snapshot(expire_at=billing.end_at), status='EXPIRED')
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    started = await service.start_if_eligible(billing, GraceReason.EXPIRED)
    assert started.session is not None
    clock.advance(timedelta(days=3, seconds=1))
    await service.reconcile()
    completed = store.only_session()
    changed_tariff = replace(
        billing,
        end_at=completed.overlay.expire_at,
        traffic_limit_bytes=billing.traffic_limit_bytes + GIB,
    )
    panel.snapshot = replace(
        panel.snapshot,
        status='EXPIRED',
        expire_at=completed.overlay.expire_at,
        traffic_limit_bytes=changed_tariff.traffic_limit_bytes,
    )
    billing_gateway.state = changed_tariff

    restarted = await service.start_if_eligible(changed_tariff, GraceReason.EXPIRED)

    assert restarted.decision is GraceStartDecision.STARTED
    assert restarted.session is not None
    assert restarted.session.id != completed.id
    assert len(store.sessions) == 2


@pytest.mark.asyncio
async def test_completed_expired_echo_dedupe_does_not_block_later_matching_incident() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = replace(make_snapshot(expire_at=billing.end_at), status='EXPIRED')
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    started = await service.start_if_eligible(billing, GraceReason.EXPIRED)
    assert started.session is not None
    clock.advance(timedelta(days=3, seconds=1))
    await service.reconcile()
    completed = store.only_session()
    clock.advance(timedelta(minutes=31))
    later_incident = replace(billing, end_at=completed.overlay.expire_at)
    billing_gateway.state = later_incident
    panel.snapshot = replace(
        panel.snapshot,
        status='EXPIRED',
        expire_at=completed.overlay.expire_at,
    )

    restarted = await service.start_if_eligible(later_incident, GraceReason.EXPIRED)

    assert restarted.decision is GraceStartDecision.STARTED
    assert restarted.session is not None
    assert restarted.session.id != completed.id
    assert len(store.sessions) == 2


@pytest.mark.asyncio
async def test_unlimited_panel_limit_becomes_exact_grace_quota_above_usage() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(
        status='expired',
        end_at=now - timedelta(days=1),
        traffic_limit_bytes=0,
        used_traffic_bytes=50 * GIB,
    )
    snapshot = make_snapshot(
        expire_at=billing.end_at,
        traffic_limit_bytes=0,
        used_traffic_bytes=billing.used_traffic_bytes,
    )
    service, _, _, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)

    result = await service.start_if_eligible(billing, GraceReason.EXPIRED)

    assert result.session is not None
    assert result.session.overlay.traffic_limit_bytes == 51 * GIB


@pytest.mark.asyncio
async def test_expired_and_exhausted_subscription_receives_temporary_bytes() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(
        status='expired',
        end_at=now - timedelta(minutes=1),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = make_snapshot(
        expire_at=billing.end_at,
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    service, _, _, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)

    result = await service.start_if_eligible(billing, GraceReason.EXPIRED)

    assert result.session is not None
    assert result.session.overlay.traffic_limit_bytes == 11 * GIB


@pytest.mark.asyncio
async def test_drain_never_activates_a_pending_session() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(minutes=1))
    snapshot = make_snapshot(expire_at=billing.end_at)
    service, store, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    panel.fail_overlay_attempts = 1

    with pytest.raises(RuntimeError):
        await service.start_if_eligible(billing, GraceReason.EXPIRED)
    result = await service.drain()

    assert result.drained == 1
    assert len(panel.applied_overlays) == 0
    assert store.only_session().completion_reason is GraceCompletionReason.DRAINED


@pytest.mark.asyncio
async def test_normal_drain_keeps_active_session_until_its_deadline() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(minutes=1))
    snapshot = make_snapshot(expire_at=billing.end_at)
    service, store, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    await service.start_if_eligible(billing, GraceReason.EXPIRED)

    before_deadline = await service.drain()
    clock.advance(timedelta(days=3, seconds=1))
    after_deadline = await service.drain()

    assert before_deadline.unchanged == 1
    assert after_deadline.timed_out == 1
    assert len(panel.applied_overlays) == 1
    assert store.only_session().completion_reason is GraceCompletionReason.TIMEOUT


@pytest.mark.asyncio
async def test_blocked_user_is_revoked_immediately() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(minutes=1))
    snapshot = make_snapshot(expire_at=billing.end_at)
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    await service.start_if_eligible(billing, GraceReason.EXPIRED)
    billing_gateway.state = replace(billing, user_status='blocked')

    result = await service.reconcile()

    assert result.revoked == 1
    assert panel.applied_billing == [billing_gateway.state]
    assert store.only_session().completion_reason is GraceCompletionReason.REVOKED


@pytest.mark.asyncio
async def test_limited_grace_fails_closed_when_panel_omits_usage() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=10),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=10 * GIB,
            used_traffic_bytes=0,
        ),
        status='LIMITED',
        traffic_is_known=False,
    )
    service, store, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)

    with pytest.raises(ValueError, match='traffic usage'):
        await service.start_if_eligible(billing, GraceReason.LIMITED)

    assert store.sessions == {}
    assert panel.applied_overlays == []


@pytest.mark.asyncio
async def test_expired_grace_fails_closed_when_panel_omits_usage() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now)
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=0,
            used_traffic_bytes=0,
        ),
        status='EXPIRED',
        traffic_is_known=False,
    )
    service, store, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)

    with pytest.raises(ValueError, match='traffic usage'):
        await service.start_if_eligible(billing, GraceReason.EXPIRED)

    assert store.sessions == {}
    assert panel.applied_overlays == []


@pytest.mark.asyncio
async def test_disabling_kind_flag_does_not_interrupt_an_open_session() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = replace(
        make_billing(status='expired', end_at=now - timedelta(minutes=1)),
        is_trial=True,
    )
    snapshot = replace(make_snapshot(expire_at=billing.end_at), status='EXPIRED')
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
        policy=make_policy(trial_enabled=True),
    )
    started = await service.start_if_eligible(billing, GraceReason.EXPIRED)
    assert started.decision is GraceStartDecision.STARTED

    service_after_restart = GraceAccessService(
        store=store,
        panel=panel,
        billing=billing_gateway,
        policy=make_policy(trial_enabled=False),
        clock=clock,
    )
    result = await service_after_restart.reconcile()

    assert result.unchanged == 1
    assert store.only_session().state is GraceSessionState.ACTIVE


@pytest.mark.asyncio
async def test_limited_grace_can_repeat_after_a_new_traffic_period() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(
        status='limited',
        end_at=now + timedelta(days=90),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    first_reset = now - timedelta(days=30)
    snapshot = replace(
        make_snapshot(
            expire_at=billing.end_at,
            traffic_limit_bytes=10 * GIB,
            used_traffic_bytes=10 * GIB,
        ),
        status='LIMITED',
        last_traffic_reset_at=first_reset,
    )
    service, store, panel, billing_gateway = make_service(
        billing=billing,
        snapshot=snapshot,
        clock=clock,
    )
    first = await service.start_if_eligible(billing, GraceReason.LIMITED)
    assert first.decision is GraceStartDecision.STARTED

    billing_gateway.state = replace(billing, status='active', used_traffic_bytes=0)
    assert (await service.reconcile()).paid == 1

    second_reset = now
    billing_gateway.state = billing
    panel.snapshot = replace(snapshot, last_traffic_reset_at=second_reset)
    second = await service.start_if_eligible(billing, GraceReason.LIMITED)

    assert second.decision is GraceStartDecision.STARTED
    assert len(store.sessions) == 2
    assert first.session is not None and second.session is not None
    assert first.session.incident_key != second.session.incident_key


@pytest.mark.asyncio
async def test_external_squad_is_detached_only_in_overlay_and_kept_in_snapshot() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    external_squad = '44444444-4444-4444-4444-444444444444'
    billing = replace(
        make_billing(status='expired', end_at=now - timedelta(minutes=1)),
        external_squad_uuid=external_squad,
    )
    snapshot = replace(
        make_snapshot(expire_at=billing.end_at),
        external_squad_uuid=external_squad,
    )
    service, _, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)

    result = await service.start_if_eligible(billing, GraceReason.EXPIRED)

    assert result.session is not None
    assert result.session.panel_before.external_squad_uuid == external_squad
    assert result.session.overlay.external_squad_uuid is None
    assert panel.snapshot.external_squad_uuid is None


@pytest.mark.asyncio
async def test_manual_panel_change_is_terminal_conflict_and_never_reapplied() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(minutes=1))
    snapshot = replace(make_snapshot(expire_at=billing.end_at), status='EXPIRED')
    service, store, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    await service.start_if_eligible(billing, GraceReason.EXPIRED)
    panel.snapshot = replace(panel.snapshot, status='DISABLED', squad_uuids=(REGULAR_SQUAD,))

    result = await service.reconcile()

    assert result.conflicts == 1
    assert len(panel.applied_overlays) == 1
    assert panel.applied_billing == []
    assert store.only_session().state is GraceSessionState.COMPLETED
    assert store.only_session().completion_reason is GraceCompletionReason.CONFLICT


@pytest.mark.asyncio
async def test_unexpected_active_panel_state_fails_closed_to_billing() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(minutes=1))
    snapshot = replace(make_snapshot(expire_at=billing.end_at), status='EXPIRED')
    service, store, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    await service.start_if_eligible(billing, GraceReason.EXPIRED)
    panel.snapshot = replace(
        panel.snapshot,
        status='ACTIVE',
        expire_at=now + timedelta(days=30),
        squad_uuids=(REGULAR_SQUAD,),
    )

    result = await service.reconcile()

    assert result.conflicts == 1
    assert panel.applied_billing == [billing]
    restoring = store.only_session()
    assert restoring.state is GraceSessionState.RESTORING
    assert restoring.completion_reason is None
    assert restoring.completed_at is None
    assert restoring.last_error == ('Unexpected ACTIVE remains different from canonical billing; restore is pending')


@pytest.mark.asyncio
async def test_restore_conflict_keeps_protection_open_until_panel_is_safe() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(minutes=1))
    snapshot = replace(make_snapshot(expire_at=billing.end_at), status='EXPIRED')
    service, store, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    await service.start_if_eligible(billing, GraceReason.EXPIRED)
    panel.restore_outcome = GraceRestoreOutcome.CONFLICT
    clock.advance(timedelta(days=3, seconds=1))

    result = await service.reconcile()

    assert result.conflicts == 1
    restoring = store.only_session()
    assert restoring.state is GraceSessionState.RESTORING
    assert restoring.completion_reason is None
    assert restoring.completed_at is None
    assert restoring.last_error is not None

    panel.restore_outcome = GraceRestoreOutcome.RESTORED
    recovered = await service.reconcile()

    assert recovered.timed_out == 1
    completed = store.only_session()
    assert completed.state is GraceSessionState.COMPLETED
    assert completed.completion_reason is GraceCompletionReason.TIMEOUT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'original_generation',
    [datetime(2026, 6, 15, 12, tzinfo=UTC), None],
)
async def test_external_reset_generation_is_revoked_fail_closed(
    original_generation: datetime | None,
) -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(minutes=1))
    snapshot = replace(
        make_snapshot(expire_at=billing.end_at),
        status='EXPIRED',
        last_traffic_reset_at=original_generation,
    )
    service, store, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    started = await service.start_if_eligible(billing, GraceReason.EXPIRED)
    assert started.session is not None
    observed_generation = now + timedelta(seconds=1)
    panel.snapshot = replace(panel.snapshot, last_traffic_reset_at=observed_generation)

    result = await service.reconcile()

    assert result.conflicts == 1
    assert panel.external_reset_revocations == [(started.session.overlay, original_generation, observed_generation)]
    completed = store.only_session()
    assert completed.state is GraceSessionState.COMPLETED
    assert completed.completion_reason is GraceCompletionReason.CONFLICT
    assert completed.last_error is not None
    assert 'access was revoked fail-closed' in completed.last_error


@pytest.mark.asyncio
async def test_external_reset_without_confirmed_revocation_keeps_protection_open() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    original_generation = now - timedelta(days=30)
    billing = make_billing(status='expired', end_at=now - timedelta(minutes=1))
    snapshot = replace(
        make_snapshot(expire_at=billing.end_at),
        status='EXPIRED',
        last_traffic_reset_at=original_generation,
    )
    service, store, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    await service.start_if_eligible(billing, GraceReason.EXPIRED)
    panel.snapshot = replace(panel.snapshot, last_traffic_reset_at=now)
    panel.external_reset_outcome = GraceRestoreOutcome.CONFLICT

    result = await service.reconcile()

    assert result.conflicts == 1
    restoring = store.only_session()
    assert restoring.state is GraceSessionState.RESTORING
    assert restoring.completion_reason is None
    assert restoring.completed_at is None
    assert restoring.last_error is not None
    assert 'revocation is not confirmed' in restoring.last_error


@pytest.mark.asyncio
async def test_unchanged_reset_generation_does_not_trigger_fail_closed_revocation() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    original_generation = now - timedelta(days=30)
    billing = make_billing(status='expired', end_at=now - timedelta(minutes=1))
    snapshot = replace(
        make_snapshot(expire_at=billing.end_at),
        status='EXPIRED',
        last_traffic_reset_at=original_generation,
    )
    service, store, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    await service.start_if_eligible(billing, GraceReason.EXPIRED)

    result = await service.reconcile()

    assert result.unchanged == 1
    assert panel.external_reset_revocations == []
    assert store.only_session().state is GraceSessionState.ACTIVE


@pytest.mark.asyncio
async def test_intentional_admin_expiry_is_suppressed_for_current_incident() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = replace(
        make_billing(status='expired', end_at=now),
        grace_suppressed_until=now,
    )
    snapshot = make_snapshot(expire_at=now)
    service, store, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)

    result = await service.start_if_eligible(billing, GraceReason.EXPIRED)

    assert result.decision is GraceStartDecision.NOT_ELIGIBLE
    assert store.sessions == {}
    assert panel.applied_overlays == []
