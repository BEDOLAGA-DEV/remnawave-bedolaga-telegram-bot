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
    GraceReason,
    GraceRestoreOutcome,
    GraceSessionState,
    GraceStartDecision,
)


GIB = 1024**3
EXPIRED_SQUAD = '11111111-1111-1111-1111-111111111111'
LIMITED_SQUAD = '22222222-2222-2222-2222-222222222222'
REGULAR_SQUAD = '33333333-3333-3333-3333-333333333333'
PANEL_UUID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'


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
        return next(
            (
                session
                for session in self.sessions.values()
                if session.subscription_id == subscription_id and session.incident_key == incident_key
            ),
            None,
        )

    async def create(self, session: GraceAccessSession) -> GraceAccessSession:
        self.sessions[session.id] = session
        return session

    async def save(self, session: GraceAccessSession) -> GraceAccessSession:
        self.sessions[session.id] = session
        return session

    async def list_open(self, *, limit: int) -> list[GraceAccessSession]:
        sessions = [session for session in self.sessions.values() if session.state is not GraceSessionState.COMPLETED]
        return sessions[:limit]

    def only_session(self) -> GraceAccessSession:
        assert len(self.sessions) == 1
        return next(iter(self.sessions.values()))


class FakePanelGateway:
    def __init__(self, snapshot: GracePanelSnapshot) -> None:
        self.snapshot = snapshot
        self.applied_overlays: list[tuple[str, GracePanelOverlay]] = []
        self.restored_snapshots: list[tuple[str, GracePanelSnapshot]] = []
        self.applied_billing: list[GraceBillingState] = []
        self.fail_overlay_attempts = 0
        self.restore_outcome = GraceRestoreOutcome.RESTORED

    async def read_snapshot(self, remnawave_uuid: str) -> GracePanelSnapshot | None:
        if remnawave_uuid != self.snapshot.remnawave_uuid:
            return None
        return self.snapshot

    async def apply_overlay(self, remnawave_uuid: str, overlay: GracePanelOverlay) -> None:
        if self.fail_overlay_attempts > 0:
            self.fail_overlay_attempts -= 1
            raise RuntimeError('temporary panel error')
        self.applied_overlays.append((remnawave_uuid, overlay))
        self.snapshot = GracePanelSnapshot(
            remnawave_uuid=remnawave_uuid,
            status=overlay.status,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            used_traffic_bytes=self.snapshot.used_traffic_bytes,
            squad_uuids=overlay.squad_uuids,
            external_squad_uuid=overlay.external_squad_uuid,
            traffic_is_known=self.snapshot.traffic_is_known,
            last_traffic_reset_at=self.snapshot.last_traffic_reset_at,
        )

    async def restore_snapshot(
        self,
        remnawave_uuid: str,
        snapshot: GracePanelSnapshot,
        expected_overlay: GracePanelOverlay,
    ) -> GraceRestoreOutcome:
        self.restored_snapshots.append((remnawave_uuid, snapshot))
        if self.restore_outcome is GraceRestoreOutcome.RESTORED:
            self.snapshot = replace(
                snapshot,
                # Consumed traffic is accounting data and is never restored.
                used_traffic_bytes=self.snapshot.used_traffic_bytes,
            )
        return self.restore_outcome

    async def apply_billing_state(self, billing: GraceBillingState) -> None:
        self.applied_billing.append(billing)


class FakeBillingGateway:
    def __init__(self, state: GraceBillingState) -> None:
        self.state = state

    async def get_subscription(self, subscription_id: int) -> GraceBillingState | None:
        if subscription_id != self.state.subscription_id:
            return None
        return self.state


def make_billing(
    *,
    status: str,
    end_at: datetime,
    traffic_limit_bytes: int = 10 * GIB,
    used_traffic_bytes: int = 3 * GIB,
) -> GraceBillingState:
    return GraceBillingState(
        subscription_id=42,
        remnawave_uuid=PANEL_UUID,
        status=status,
        end_at=end_at,
        traffic_limit_bytes=traffic_limit_bytes,
        used_traffic_bytes=used_traffic_bytes,
        device_limit=2,
        squad_uuids=(REGULAR_SQUAD,),
    )


def make_snapshot(
    *,
    expire_at: datetime,
    traffic_limit_bytes: int = 10 * GIB,
    used_traffic_bytes: int = 3 * GIB,
) -> GracePanelSnapshot:
    return GracePanelSnapshot(
        remnawave_uuid=PANEL_UUID,
        status='DISABLED',
        expire_at=expire_at,
        traffic_limit_bytes=traffic_limit_bytes,
        used_traffic_bytes=used_traffic_bytes,
        squad_uuids=(REGULAR_SQUAD,),
    )


def make_service(
    *,
    billing: GraceBillingState,
    snapshot: GracePanelSnapshot,
    clock: MutableClock,
) -> tuple[GraceAccessService, MemoryGraceStore, FakePanelGateway, FakeBillingGateway]:
    store = MemoryGraceStore()
    panel = FakePanelGateway(snapshot)
    billing_gateway = FakeBillingGateway(billing)
    policy = GraceAccessPolicy(
        duration=timedelta(days=3),
        expired_squad_uuid=EXPIRED_SQUAD,
        limited_squad_uuid=LIMITED_SQUAD,
        expired_traffic_bytes=GIB,
        limited_traffic_bytes=GIB,
    )
    service = GraceAccessService(
        store=store,
        panel=panel,
        billing=billing_gateway,
        policy=policy,
        clock=clock,
    )
    return service, store, panel, billing_gateway


@pytest.mark.asyncio
async def test_expired_grace_changes_only_panel_overlay() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(days=1))
    snapshot = make_snapshot(expire_at=billing.end_at)
    service, store, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)

    result = await service.start_if_eligible(billing, GraceReason.EXPIRED)

    assert result.decision is GraceStartDecision.STARTED
    assert result.session is not None
    assert result.session.state is GraceSessionState.ACTIVE
    assert result.session.billing_before is billing
    assert result.session.panel_before is snapshot
    assert result.session.overlay.status == 'ACTIVE'
    assert result.session.overlay.expire_at == now + timedelta(days=3)
    assert result.session.overlay.traffic_limit_bytes == snapshot.traffic_limit_bytes
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
    assert panel.restored_snapshots == [(PANEL_UUID, snapshot)]
    completed = store.only_session()
    assert completed.state is GraceSessionState.COMPLETED
    assert completed.completion_reason is GraceCompletionReason.TIMEOUT
    assert panel.snapshot.used_traffic_bytes == 7 * GIB

    repeated = await service.start_if_eligible(billing, GraceReason.EXPIRED)
    assert repeated.decision is GraceStartDecision.ALREADY_GRANTED
    assert len(panel.applied_overlays) == 1


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
        'status': 'ACTIVE',
        'expireAt': overlay.expire_at.isoformat(),
        'trafficLimitBytes': overlay.traffic_limit_bytes,
        'activeInternalSquads': [{'uuid': EXPIRED_SQUAD}],
    }
    real_update = {
        'status': 'ACTIVE',
        'expireAt': (now + timedelta(days=30)).isoformat(),
        'trafficLimitBytes': 20 * GIB,
        'activeInternalSquads': [{'uuid': REGULAR_SQUAD}],
    }

    assert await service.should_suppress_webhook(42, 'user.modified', grace_echo) is False
    assert await service.should_suppress_webhook(42, 'user.modified', real_update) is False
    assert await service.should_suppress_webhook(42, 'user.enabled', {}) is True
    assert await service.should_suppress_webhook(42, 'user.enabled', grace_echo) is True
    assert await service.should_suppress_webhook(42, 'user.disabled', grace_echo) is False

    billing_gateway.state = replace(billing, status='active', end_at=now + timedelta(days=30))
    # A delayed echo from the old overlay must still be suppressed until the
    # reconciliation transaction closes the persisted grace session.
    assert await service.should_suppress_webhook(42, 'user.enabled', grace_echo) is True


@pytest.mark.asyncio
async def test_unlimited_panel_limit_remains_unlimited() -> None:
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
    assert result.session.overlay.traffic_limit_bytes == 0


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
    snapshot = make_snapshot(expire_at=billing.end_at)
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
    snapshot = make_snapshot(expire_at=billing.end_at)
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
    assert store.only_session().state is GraceSessionState.COMPLETED
    assert store.only_session().completion_reason is GraceCompletionReason.CONFLICT


@pytest.mark.asyncio
async def test_restore_conflict_is_terminal_instead_of_blocking_drain_forever() -> None:
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    clock = MutableClock(now)
    billing = make_billing(status='expired', end_at=now - timedelta(minutes=1))
    snapshot = make_snapshot(expire_at=billing.end_at)
    service, store, panel, _ = make_service(billing=billing, snapshot=snapshot, clock=clock)
    await service.start_if_eligible(billing, GraceReason.EXPIRED)
    panel.restore_outcome = GraceRestoreOutcome.CONFLICT
    clock.advance(timedelta(days=3, seconds=1))

    result = await service.reconcile()

    assert result.conflicts == 1
    assert store.only_session().state is GraceSessionState.COMPLETED
    assert store.only_session().last_error is not None


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
