"""Business rules for temporary restricted access after subscription exhaustion.

This module intentionally contains no SQLAlchemy, scheduler, webhook, or Remnawave
SDK wiring.  Those integrations are represented by small protocols so the grace
rules remain testable and so future upstream updates only need a few stable entry
points.

The billing subscription always remains the source of truth.  Grace is a
temporary overlay in Remnawave and is recorded as a separate session.  In
particular, this service never resets used traffic and never extends the billing
subscription itself.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

import structlog


logger = structlog.get_logger(__name__)


class GraceReason(StrEnum):
    EXPIRED = 'expired'
    LIMITED = 'limited'


class GraceAccessMode(StrEnum):
    """Runtime mode selected through ``GRACE_ACCESS_MODE``."""

    DISABLED = 'false'
    OBSERVE = 'observe'
    ACTIVE = 'true'
    DRAIN = 'drain'

    @classmethod
    def parse(cls, value: object) -> GraceAccessMode:
        normalized = str(getattr(value, 'value', value)).strip().lower()
        try:
            return cls(normalized)
        except ValueError as error:
            allowed = ', '.join(mode.value for mode in cls)
            raise ValueError(f'GRACE_ACCESS_MODE must be one of: {allowed}') from error


class GraceSessionState(StrEnum):
    PENDING = 'pending'
    ACTIVE = 'active'
    RESTORING = 'restoring'
    COMPLETED = 'completed'


class GraceCompletionReason(StrEnum):
    PAID = 'paid'
    TIMEOUT = 'timeout'
    DRAINED = 'drained'
    CONFLICT = 'conflict'
    REVOKED = 'revoked'


class GraceRestoreOutcome(StrEnum):
    """Result of compare-and-set restoration in Remnawave."""

    RESTORED = 'restored'
    ALREADY_RESTORED = 'already_restored'
    CONFLICT = 'conflict'


class GraceStartDecision(StrEnum):
    STARTED = 'started'
    RETRIED = 'retried'
    ALREADY_ACTIVE = 'already_active'
    ALREADY_GRANTED = 'already_granted'
    NOT_ELIGIBLE = 'not_eligible'
    PANEL_USER_NOT_FOUND = 'panel_user_not_found'
    SUPERSEDED = 'superseded'
    OBSERVED = 'observed'


@dataclass(frozen=True, slots=True)
class GraceAccessPolicy:
    """Configuration required by the core grace rules."""

    duration: timedelta
    expired_squad_uuid: str
    limited_squad_uuid: str
    expired_traffic_bytes: int = 1024**3
    limited_traffic_bytes: int = 1024**3
    reconcile_batch_size: int = 200

    def __post_init__(self) -> None:
        if self.duration <= timedelta(0):
            raise ValueError('Grace duration must be positive')
        if self.expired_traffic_bytes < 0 or self.limited_traffic_bytes < 0:
            raise ValueError('Grace traffic must not be negative')
        if self.reconcile_batch_size < 1:
            raise ValueError('Grace reconcile batch size must be positive')

    def squad_for(self, reason: GraceReason) -> str:
        squad_uuid = self.expired_squad_uuid if reason is GraceReason.EXPIRED else self.limited_squad_uuid
        if not squad_uuid.strip():
            raise ValueError(f'Grace squad UUID for {reason.value} is required when GRACE_ACCESS_MODE=true')
        return squad_uuid

    def traffic_for(self, reason: GraceReason) -> int:
        if reason is GraceReason.EXPIRED:
            return self.expired_traffic_bytes
        return self.limited_traffic_bytes


@dataclass(frozen=True, slots=True)
class GraceBillingState:
    """Canonical subscription data owned by the bot billing database."""

    subscription_id: int
    remnawave_uuid: str | None
    status: str
    end_at: datetime | None
    traffic_limit_bytes: int
    used_traffic_bytes: int
    device_limit: int | None
    squad_uuids: tuple[str, ...]
    external_squad_uuid: str | None = None
    is_trial: bool = False
    is_daily: bool = False
    is_free_tariff: bool = False
    user_status: str = 'active'
    grace_suppressed_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class GracePanelSnapshot:
    """Remnawave values changed by grace and therefore restored on timeout.

    ``used_traffic_bytes`` is captured for calculating a temporary limit, but it
    must never be restored: traffic consumed during grace is real traffic.
    """

    remnawave_uuid: str
    status: str
    expire_at: datetime | None
    traffic_limit_bytes: int
    used_traffic_bytes: int
    squad_uuids: tuple[str, ...]
    external_squad_uuid: str | None = None
    traffic_is_known: bool = True
    last_traffic_reset_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GracePanelOverlay:
    """Exact temporary state expected in Remnawave while grace is active."""

    status: str
    expire_at: datetime
    traffic_limit_bytes: int
    squad_uuids: tuple[str, ...]
    external_squad_uuid: str | None = None


@dataclass(frozen=True, slots=True)
class GraceAccessSession:
    """Persistence-neutral grace session entity."""

    id: str
    subscription_id: int
    remnawave_uuid: str
    reason: GraceReason
    incident_key: str
    state: GraceSessionState
    billing_before: GraceBillingState
    panel_before: GracePanelSnapshot
    overlay: GracePanelOverlay
    started_at: datetime
    grace_until: datetime
    updated_at: datetime
    completion_reason: GraceCompletionReason | None = None
    completed_at: datetime | None = None
    last_error: str | None = None
    version: int = 1


@dataclass(frozen=True, slots=True)
class GraceStartResult:
    decision: GraceStartDecision
    session: GraceAccessSession | None = None


@dataclass(frozen=True, slots=True)
class GraceReconcileResult:
    inspected: int = 0
    activated: int = 0
    paid: int = 0
    timed_out: int = 0
    drained: int = 0
    revoked: int = 0
    conflicts: int = 0
    repaired: int = 0
    unchanged: int = 0
    errors: int = 0


class GraceSessionStore(Protocol):
    """Persistence adapter implemented in the next integration step."""

    async def get_open(self, subscription_id: int) -> GraceAccessSession | None: ...

    async def get_by_incident(self, subscription_id: int, incident_key: str) -> GraceAccessSession | None: ...

    async def create(self, session: GraceAccessSession) -> GraceAccessSession: ...

    async def save(self, session: GraceAccessSession) -> GraceAccessSession: ...

    async def list_open(self, *, limit: int) -> Sequence[GraceAccessSession]: ...


class GracePanelGateway(Protocol):
    """Remnawave adapter implemented in the next integration step."""

    async def read_snapshot(self, remnawave_uuid: str) -> GracePanelSnapshot | None: ...

    async def apply_overlay(self, remnawave_uuid: str, overlay: GracePanelOverlay) -> None: ...

    async def restore_snapshot(
        self,
        remnawave_uuid: str,
        snapshot: GracePanelSnapshot,
        expected_overlay: GracePanelOverlay,
    ) -> GraceRestoreOutcome: ...

    async def apply_billing_state(self, billing: GraceBillingState) -> None: ...


class GraceBillingGateway(Protocol):
    """Read-only adapter for the bot's canonical subscription state."""

    async def get_subscription(self, subscription_id: int) -> GraceBillingState | None: ...


class GraceAccessService:
    """Orchestrates one-shot grace overlays without mutating billing data."""

    def __init__(
        self,
        *,
        store: GraceSessionStore,
        panel: GracePanelGateway,
        billing: GraceBillingGateway,
        policy: GraceAccessPolicy,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._panel = panel
        self._billing = billing
        self._policy = policy
        self._clock = clock or _utc_now

    async def start_if_eligible(
        self,
        billing: GraceBillingState,
        reason: GraceReason,
    ) -> GraceStartResult:
        """Create and apply one grace session for one billing incident."""
        if not billing_is_eligible(billing, reason):
            return GraceStartResult(GraceStartDecision.NOT_ELIGIBLE)
        if not billing.remnawave_uuid:
            return GraceStartResult(GraceStartDecision.PANEL_USER_NOT_FOUND)

        open_session = await self._store.get_open(billing.subscription_id)
        if open_session:
            if open_session.state is GraceSessionState.PENDING:
                active_session = await self._activate_pending(open_session)
                decision = (
                    GraceStartDecision.RETRIED
                    if active_session.state is GraceSessionState.ACTIVE
                    else GraceStartDecision.SUPERSEDED
                )
                return GraceStartResult(decision, active_session)
            return GraceStartResult(GraceStartDecision.ALREADY_ACTIVE, open_session)

        panel_snapshot = await self._panel.read_snapshot(billing.remnawave_uuid)
        if not panel_snapshot:
            return GraceStartResult(GraceStartDecision.PANEL_USER_NOT_FOUND)
        if not panel_status_matches_reason(panel_snapshot.status, reason):
            logger.warning(
                'Grace candidate panel status no longer matches incident',
                subscription_id=billing.subscription_id,
                reason=reason.value,
                panel_status=panel_snapshot.status,
            )
            return GraceStartResult(GraceStartDecision.SUPERSEDED)

        incident_key = build_incident_key(
            billing,
            reason,
            last_traffic_reset_at=panel_snapshot.last_traffic_reset_at,
        )
        previous_session = await self._store.get_by_incident(billing.subscription_id, incident_key)
        if previous_session:
            return GraceStartResult(GraceStartDecision.ALREADY_GRANTED, previous_session)

        now = _as_utc(self._clock())
        if billing.used_traffic_bytes > panel_snapshot.used_traffic_bytes:
            panel_snapshot = replace(
                panel_snapshot,
                used_traffic_bytes=billing.used_traffic_bytes,
            )
        overlay = build_panel_overlay(panel_snapshot, reason, self._policy, now=now)
        pending_session = GraceAccessSession(
            id=str(uuid4()),
            subscription_id=billing.subscription_id,
            remnawave_uuid=billing.remnawave_uuid,
            reason=reason,
            incident_key=incident_key,
            state=GraceSessionState.PENDING,
            billing_before=billing,
            panel_before=panel_snapshot,
            overlay=overlay,
            started_at=now,
            grace_until=overlay.expire_at,
            updated_at=now,
        )
        pending_session = await self._store.create(pending_session)
        if pending_session.incident_key != incident_key:
            return GraceStartResult(GraceStartDecision.ALREADY_ACTIVE, pending_session)
        if pending_session.state is GraceSessionState.COMPLETED:
            return GraceStartResult(GraceStartDecision.ALREADY_GRANTED, pending_session)
        if pending_session.state is GraceSessionState.ACTIVE:
            return GraceStartResult(GraceStartDecision.ALREADY_ACTIVE, pending_session)
        if pending_session.state is GraceSessionState.RESTORING:
            return GraceStartResult(GraceStartDecision.ALREADY_ACTIVE, pending_session)
        active_session = await self._activate_pending(pending_session)
        decision = (
            GraceStartDecision.STARTED
            if active_session.state is GraceSessionState.ACTIVE
            else GraceStartDecision.SUPERSEDED
        )
        return GraceStartResult(decision, active_session)

    async def payment_has_recovered(self, subscription_id: int) -> bool:
        """Return whether fresh canonical billing represents a real recovery."""
        session = await self._store.get_open(subscription_id)
        if not session:
            return False

        billing = await self._billing.get_subscription(subscription_id)
        return bool(billing and billing_has_recovered(session, billing))

    async def complete_after_payment(
        self,
        subscription_id: int,
        *,
        apply_billing_state: bool = True,
    ) -> bool:
        """End grace after payment and optionally push canonical panel values.

        This method is safe to call explicitly after a successful payment.  The
        periodic reconciliation path performs the same operation as a fallback.
        A caller that already applied and verified the canonical panel update
        under the same lock may set ``apply_billing_state=False`` to avoid a
        duplicate Remnawave PATCH.
        """
        session = await self._store.get_open(subscription_id)
        if not session:
            return False

        billing = await self._billing.get_subscription(subscription_id)
        if not billing or not billing_has_recovered(session, billing):
            return False

        if apply_billing_state:
            await self._panel.apply_billing_state(billing)
        await self._complete(session, GraceCompletionReason.PAID)
        return True

    async def reconcile(self, *, limit: int | None = None) -> GraceReconcileResult:
        """Repair pending sessions and finish paid or timed-out sessions."""
        return await self._run_reconciliation(limit=limit, activate_pending=True, force_restore=False)

    async def drain(
        self,
        *,
        limit: int | None = None,
        force_restore: bool = False,
    ) -> GraceReconcileResult:
        """Finish existing sessions without ever granting a new overlay.

        Normal drain lets ACTIVE sessions run to their original ``grace_until``.
        ``force_restore`` is reserved for the explicit emergency CLI and restores
        ACTIVE sessions immediately.  PENDING sessions are never activated.
        """
        return await self._run_reconciliation(
            limit=limit,
            activate_pending=False,
            force_restore=force_restore,
        )

    async def _run_reconciliation(
        self,
        *,
        limit: int | None,
        activate_pending: bool,
        force_restore: bool,
    ) -> GraceReconcileResult:
        sessions = await self._store.list_open(limit=limit or self._policy.reconcile_batch_size)
        result = GraceReconcileResult(inspected=len(sessions))

        for session in sessions:
            try:
                action = await self._reconcile_one(
                    session,
                    activate_pending=activate_pending,
                    force_restore=force_restore,
                )
            except Exception as error:
                await self._remember_error(session.subscription_id, error)
                logger.exception(
                    'Grace reconciliation failed',
                    subscription_id=session.subscription_id,
                    grace_session_id=session.id,
                )
                result = replace(result, errors=result.errors + 1)
                continue

            if action == 'activated':
                result = replace(result, activated=result.activated + 1)
            elif action == 'repaired':
                result = replace(result, repaired=result.repaired + 1)
            elif action == GraceCompletionReason.PAID.value:
                result = replace(result, paid=result.paid + 1)
            elif action == GraceCompletionReason.TIMEOUT.value:
                result = replace(result, timed_out=result.timed_out + 1)
            elif action == GraceCompletionReason.DRAINED.value:
                result = replace(result, drained=result.drained + 1)
            elif action == GraceCompletionReason.REVOKED.value:
                result = replace(result, revoked=result.revoked + 1)
            elif action == GraceCompletionReason.CONFLICT.value:
                result = replace(result, conflicts=result.conflicts + 1)
            else:
                result = replace(result, unchanged=result.unchanged + 1)

        return result

    async def should_suppress_webhook(
        self,
        subscription_id: int,
        event_name: str,
        payload: Mapping[str, Any],
    ) -> bool:
        """Return whether a panel event is an echo of the active grace overlay."""
        session = await self._store.get_open(subscription_id)
        if not session:
            return False

        normalized_event = event_name.strip().lower()

        # A real administrative disable must always win.  We deliberately let
        # restore-generated ``user.disabled`` pass too: changing EXPIRED to
        # DISABLED is less dangerous than hiding a real revocation and later
        # re-enabling it.
        if normalized_event == 'user.disabled':
            return False

        # user.modified is handled field-by-field by the webhook service: only
        # grace-owned fields are masked while usage and URLs keep synchronizing.
        if normalized_event == 'user.modified':
            return False

        # These transitions are expected consequences of enabling/consuming/
        # expiring the temporary overlay. Billing remains authoritative; a real
        # manual panel change is still preserved because reconciliation never
        # re-applies a mismatching overlay. Payload completeness varies between
        # Remnawave releases, so these cannot rely on optional fields.
        return normalized_event in {'user.enabled', 'user.expired', 'user.limited'}

    async def _activate_pending(self, session: GraceAccessSession) -> GraceAccessSession:
        now = _as_utc(self._clock())
        latest_billing = await self._billing.get_subscription(session.subscription_id)

        if latest_billing and billing_has_recovered(session, latest_billing):
            await self._panel.apply_billing_state(latest_billing)
            return await self._complete(session, GraceCompletionReason.PAID)

        if latest_billing is None or billing_is_revoked(latest_billing):
            completion_reason = GraceCompletionReason.REVOKED
            action = await self._restore_and_complete(session, completion_reason)
            return action[1]

        if (
            now >= _as_utc(session.grace_until)
            or not billing_is_eligible(latest_billing, session.reason)
            or not billing_still_matches_session(session, latest_billing)
        ):
            action = await self._restore_and_complete(session, GraceCompletionReason.CONFLICT)
            return action[1]

        current_panel = await self._panel.read_snapshot(session.remnawave_uuid)
        if current_panel is None:
            return await self._complete(
                session,
                GraceCompletionReason.CONFLICT,
                last_error='Remnawave user disappeared before pending grace could be activated',
            )

        overlay_is_already_applied = panel_matches_overlay(
            current_panel,
            session.overlay,
            now=now,
        )
        if not overlay_is_already_applied and not panel_is_safe_pending_source(
            current_panel,
            session.panel_before,
            session.overlay,
        ):
            # A retry is allowed only from the original snapshot, the exact
            # overlay, or the one known intermediate produced by our external
            # squad preflight.  Any other state may be a manual/emergency
            # revocation. Canonical billing is fail-closed and must win.
            try:
                await self._panel.apply_billing_state(latest_billing)
            except Exception as error:
                failed_session = replace(
                    session,
                    updated_at=_as_utc(self._clock()),
                    last_error=_error_text(error),
                )
                await self._store.save(failed_session)
                raise
            return await self._complete(
                session,
                GraceCompletionReason.CONFLICT,
                last_error='Remnawave changed while grace was pending; overlay was not re-applied',
            )

        if not overlay_is_already_applied:
            try:
                await self._panel.apply_overlay(session.remnawave_uuid, session.overlay)
            except Exception as error:
                failed_session = replace(
                    session,
                    updated_at=_as_utc(self._clock()),
                    last_error=_error_text(error),
                )
                await self._store.save(failed_session)
                raise

        latest_billing = await self._billing.get_subscription(session.subscription_id)
        if latest_billing and billing_has_recovered(session, latest_billing):
            await self._panel.apply_billing_state(latest_billing)
            return await self._complete(session, GraceCompletionReason.PAID)
        if latest_billing is None or billing_is_revoked(latest_billing):
            if latest_billing is not None:
                await self._panel.apply_billing_state(latest_billing)
                return await self._complete(session, GraceCompletionReason.REVOKED)
            _, completed = await self._restore_and_complete(session, GraceCompletionReason.REVOKED)
            return completed
        if not billing_is_eligible(latest_billing, session.reason) or not billing_still_matches_session(
            session, latest_billing
        ):
            _, completed = await self._restore_and_complete(session, GraceCompletionReason.CONFLICT)
            return completed

        active_session = replace(
            session,
            state=GraceSessionState.ACTIVE,
            updated_at=_as_utc(self._clock()),
            last_error=None,
        )
        return await self._store.save(active_session)

    async def _reconcile_one(
        self,
        session: GraceAccessSession,
        *,
        activate_pending: bool,
        force_restore: bool,
    ) -> str:
        billing = await self._billing.get_subscription(session.subscription_id)
        if billing and billing_has_recovered(session, billing):
            await self._panel.apply_billing_state(billing)
            await self._complete(session, GraceCompletionReason.PAID)
            return GraceCompletionReason.PAID.value

        if billing is None or billing_is_revoked(billing):
            if billing is not None:
                await self._panel.apply_billing_state(billing)
                latest_billing = await self._billing.get_subscription(session.subscription_id)
                if latest_billing and billing_has_recovered(session, latest_billing):
                    await self._panel.apply_billing_state(latest_billing)
                    await self._complete(session, GraceCompletionReason.PAID)
                    return GraceCompletionReason.PAID.value
                await self._complete(session, GraceCompletionReason.REVOKED)
                return GraceCompletionReason.REVOKED.value
            action, _ = await self._restore_and_complete(session, GraceCompletionReason.REVOKED)
            return action

        # The recipient or canonical incident changed while grace was open
        # (admin cancellation/shortening, tariff change, UUID replacement,
        # squads/device/limit change).  Never continue an overlay based on a
        # stale snapshot.  When the same panel user still belongs to billing,
        # canonical billing wins immediately; otherwise restore the old user by
        # compare-and-set and leave unrelated panel changes untouched.
        if not billing_is_eligible(billing, session.reason) or not billing_still_matches_session(session, billing):
            if billing.remnawave_uuid == session.remnawave_uuid:
                await self._panel.apply_billing_state(billing)
                await self._complete(session, GraceCompletionReason.CONFLICT)
                return GraceCompletionReason.CONFLICT.value
            action, _ = await self._restore_and_complete(session, GraceCompletionReason.CONFLICT)
            return action

        now = _as_utc(self._clock())
        if session.state is GraceSessionState.PENDING:
            if activate_pending and not force_restore and now < _as_utc(session.grace_until):
                activated_session = await self._activate_pending(session)
                if activated_session.state is GraceSessionState.ACTIVE:
                    return 'activated'
                return (activated_session.completion_reason or GraceCompletionReason.CONFLICT).value

            completion_reason = (
                GraceCompletionReason.DRAINED
                if force_restore or now < _as_utc(session.grace_until)
                else GraceCompletionReason.TIMEOUT
            )
            action, _ = await self._restore_and_complete(session, completion_reason)
            return action

        if session.state is GraceSessionState.ACTIVE and not force_restore and now < _as_utc(session.grace_until):
            current_panel = await self._panel.read_snapshot(session.remnawave_uuid)
            if current_panel is None:
                await self._complete(session, GraceCompletionReason.CONFLICT)
                return GraceCompletionReason.CONFLICT.value
            if panel_matches_overlay(current_panel, session.overlay, now=now):
                return 'unchanged'

            # Any unexpected panel difference can be an emergency/manual
            # revocation. Never blindly re-apply the overlay, including in
            # drain. An unexpected ACTIVE state is different: leaving it could
            # grant unrestricted access after a crashed/stale renewal PATCH, so
            # fail closed to the current canonical billing state.
            if _normalize_status(current_panel.status) == 'active':
                await self._panel.apply_billing_state(billing)
                await self._complete(
                    session,
                    GraceCompletionReason.CONFLICT,
                    last_error='Unexpected active Remnawave state was replaced by canonical billing',
                )
                return GraceCompletionReason.CONFLICT.value

            await self._complete(
                session,
                GraceCompletionReason.CONFLICT,
                last_error='Remnawave state changed outside grace; overlay was not re-applied',
            )
            return GraceCompletionReason.CONFLICT.value

        completion_reason = GraceCompletionReason.DRAINED if force_restore else GraceCompletionReason.TIMEOUT
        action, _ = await self._restore_and_complete(session, completion_reason)
        return action

    async def _restore_and_complete(
        self,
        session: GraceAccessSession,
        completion_reason: GraceCompletionReason,
    ) -> tuple[str, GraceAccessSession]:
        now = _as_utc(self._clock())
        restoring_session = session
        if session.state is not GraceSessionState.RESTORING:
            restoring_session = replace(
                session,
                state=GraceSessionState.RESTORING,
                updated_at=now,
                last_error=None,
            )
            restoring_session = await self._store.save(restoring_session)
            if restoring_session.state is GraceSessionState.COMPLETED:
                reason = restoring_session.completion_reason or GraceCompletionReason.CONFLICT
                return reason.value, restoring_session

        latest_billing = await self._billing.get_subscription(session.subscription_id)
        if latest_billing and billing_has_recovered(restoring_session, latest_billing):
            await self._panel.apply_billing_state(latest_billing)
            completed = await self._complete(restoring_session, GraceCompletionReason.PAID)
            return GraceCompletionReason.PAID.value, completed

        outcome = await self._panel.restore_snapshot(
            restoring_session.remnawave_uuid,
            restoring_session.panel_before,
            restoring_session.overlay,
        )

        # Payment may land after the pre-restore check.  Paid billing always wins
        # over an old snapshot, even if the restore PATCH has already succeeded.
        latest_billing = await self._billing.get_subscription(session.subscription_id)
        if latest_billing and billing_has_recovered(restoring_session, latest_billing):
            await self._panel.apply_billing_state(latest_billing)
            completed = await self._complete(restoring_session, GraceCompletionReason.PAID)
            return GraceCompletionReason.PAID.value, completed

        if outcome is GraceRestoreOutcome.CONFLICT:
            completed = await self._complete(
                restoring_session,
                GraceCompletionReason.CONFLICT,
                last_error='Remnawave state changed outside grace; automatic restore was not applied',
            )
            return GraceCompletionReason.CONFLICT.value, completed

        completed = await self._complete(restoring_session, completion_reason)
        return completion_reason.value, completed

    async def _complete(
        self,
        session: GraceAccessSession,
        completion_reason: GraceCompletionReason,
        *,
        last_error: str | None = None,
    ) -> GraceAccessSession:
        now = _as_utc(self._clock())
        completed_session = replace(
            session,
            state=GraceSessionState.COMPLETED,
            completion_reason=completion_reason,
            completed_at=now,
            updated_at=now,
            last_error=last_error,
        )
        return await self._store.save(completed_session)

    async def _remember_error(self, subscription_id: int, error: Exception) -> None:
        session = await self._store.get_open(subscription_id)
        if not session:
            return
        await self._store.save(
            replace(
                session,
                updated_at=_as_utc(self._clock()),
                last_error=_error_text(error),
            )
        )


def build_incident_key(
    billing: GraceBillingState,
    reason: GraceReason,
    *,
    last_traffic_reset_at: datetime | None = None,
) -> str:
    """Build a stable identifier so one incident receives grace only once."""
    end_at = _as_utc(billing.end_at).isoformat() if billing.end_at else 'none'
    if reason is GraceReason.EXPIRED:
        return f'{reason.value}:{end_at}'
    reset_at = _as_utc(last_traffic_reset_at).isoformat() if last_traffic_reset_at else 'unknown'
    return f'{reason.value}:{end_at}:{billing.traffic_limit_bytes}:{reset_at}'


def billing_still_matches_session(
    session: GraceAccessSession,
    current: GraceBillingState,
) -> bool:
    """Compare canonical fields that identify the incident without panel metadata."""
    before = session.billing_before
    if current.remnawave_uuid != session.remnawave_uuid:
        return False
    if _normalize_status(current.status) != session.reason.value:
        return False
    if not _datetimes_equal(current.end_at, before.end_at):
        return False
    return (
        current.traffic_limit_bytes == before.traffic_limit_bytes
        and current.device_limit == before.device_limit
        and set(current.squad_uuids) == set(before.squad_uuids)
        and current.external_squad_uuid == before.external_squad_uuid
        and current.is_trial == before.is_trial
        and current.is_daily == before.is_daily
        and current.is_free_tariff == before.is_free_tariff
    )


def billing_is_eligible(billing: GraceBillingState, reason: GraceReason) -> bool:
    """Reject trial, daily, free, blocked and otherwise non-billable accounts."""
    suppressed = False
    if billing.grace_suppressed_until is not None:
        suppressed_until = _as_utc(billing.grace_suppressed_until)
        suppressed = billing.end_at is None or _as_utc(billing.end_at) <= suppressed_until
    return (
        _normalize_status(billing.status) == reason.value
        and _normalize_status(billing.user_status) == 'active'
        and not billing.is_trial
        and not billing.is_daily
        and not billing.is_free_tariff
        and not suppressed
    )


def billing_is_revoked(billing: GraceBillingState) -> bool:
    """Return whether grace must be removed immediately for safety."""
    return _normalize_status(billing.user_status) != 'active' or _normalize_status(billing.status) == 'disabled'


def panel_status_matches_reason(status: str, reason: GraceReason) -> bool:
    normalized = _normalize_status(status)
    if reason is GraceReason.EXPIRED:
        return normalized in {'expired', 'disabled', 'limited'}
    return normalized == 'limited'


def build_panel_overlay(
    snapshot: GracePanelSnapshot,
    reason: GraceReason,
    policy: GraceAccessPolicy,
    *,
    now: datetime,
) -> GracePanelOverlay:
    """Calculate temporary panel values without resetting consumed traffic."""
    if reason is GraceReason.LIMITED and not snapshot.traffic_is_known:
        raise ValueError('Remnawave did not return traffic usage for a LIMITED user')

    traffic_grant = policy.traffic_for(reason)
    if snapshot.traffic_limit_bytes == 0:
        temporary_limit = 0  # Remnawave uses zero as an unlimited traffic limit.
    elif traffic_grant == 0:
        temporary_limit = snapshot.traffic_limit_bytes
    else:
        temporary_limit = max(
            snapshot.traffic_limit_bytes,
            snapshot.used_traffic_bytes + traffic_grant,
        )

    return GracePanelOverlay(
        status='ACTIVE',
        expire_at=_as_utc(now) + policy.duration,
        traffic_limit_bytes=temporary_limit,
        squad_uuids=(policy.squad_for(reason),),
        # External squads can provide unrestricted access independently of the
        # internal Telegram-only squad, so grace must temporarily detach them.
        external_squad_uuid=None,
    )


def billing_has_recovered(session: GraceAccessSession, current: GraceBillingState) -> bool:
    """Detect a real renewal or traffic purchase in the canonical billing state."""
    if _normalize_status(current.user_status) != 'active':
        return False
    if _normalize_status(current.status) not in {'active', 'trial'}:
        return False

    before = session.billing_before
    if _is_later(current.end_at, before.end_at):
        return True
    if before.traffic_limit_bytes > 0 and current.traffic_limit_bytes == 0:
        return True
    if before.traffic_limit_bytes > 0 and current.traffic_limit_bytes > before.traffic_limit_bytes:
        return True
    return session.reason is GraceReason.LIMITED and current.used_traffic_bytes < before.used_traffic_bytes


def panel_matches_overlay(
    snapshot: GracePanelSnapshot,
    overlay: GracePanelOverlay,
    *,
    now: datetime,
) -> bool:
    """Match only fields controlled by grace; used traffic is intentionally ignored."""
    normalized_status = _normalize_status(snapshot.status)
    expected_expire = _as_utc(overlay.expire_at)
    status_matches = normalized_status in {'active', 'limited'}
    if _as_utc(now) >= expected_expire:
        status_matches = normalized_status in {'active', 'limited', 'expired', 'disabled'}

    return (
        status_matches
        and snapshot.expire_at is not None
        and abs((_as_utc(snapshot.expire_at) - expected_expire).total_seconds()) <= 2
        and snapshot.traffic_limit_bytes == overlay.traffic_limit_bytes
        and set(snapshot.squad_uuids) == set(overlay.squad_uuids)
        and snapshot.external_squad_uuid == overlay.external_squad_uuid
    )


def panel_is_safe_pending_source(
    current: GracePanelSnapshot,
    before: GracePanelSnapshot,
    overlay: GracePanelOverlay,
) -> bool:
    """Recognize only states that this PENDING activation could have produced.

    Used traffic is intentionally ignored because it is monotonic accounting
    data.  The sole accepted partial state is an otherwise unchanged original
    snapshot whose external squad has already been detached by the gateway's
    preflight PATCH.
    """
    unchanged_except_external = (
        current.remnawave_uuid == before.remnawave_uuid
        and _normalize_status(current.status) == _normalize_status(before.status)
        and _datetimes_equal(current.expire_at, before.expire_at)
        and current.traffic_limit_bytes == before.traffic_limit_bytes
        and set(current.squad_uuids) == set(before.squad_uuids)
    )
    return unchanged_except_external and current.external_squad_uuid in {
        before.external_squad_uuid,
        overlay.external_squad_uuid,
    }


def webhook_matches_overlay_event(
    payload: Mapping[str, Any],
    overlay: GracePanelOverlay,
    event_name: str,
) -> bool:
    """Require strong overlay markers before hiding a status webhook."""
    status = _normalize_status(payload.get('status', ''))
    expected_statuses = {
        'user.enabled': {'active'},
        'user.expired': {'expired', 'disabled'},
        'user.limited': {'limited'},
    }
    if status not in expected_statuses.get(event_name, set()):
        return False

    expire_at = _parse_datetime(payload.get('expireAt'))
    if not expire_at or abs((expire_at - _as_utc(overlay.expire_at)).total_seconds()) > 2:
        return False

    try:
        if int(payload.get('trafficLimitBytes')) != overlay.traffic_limit_bytes:
            return False
    except (TypeError, ValueError):
        return False

    if 'activeInternalSquads' not in payload:
        return False
    if set(_extract_squad_uuids(payload.get('activeInternalSquads'))) != set(overlay.squad_uuids):
        return False

    return payload.get('externalSquadUuid') == overlay.external_squad_uuid


def webhook_matches_overlay(payload: Mapping[str, Any], overlay: GracePanelOverlay) -> bool:
    """Strictly match a user.modified echo without hiding unrelated updates."""
    status = payload.get('status')
    if status is not None and _normalize_status(status) != 'active':
        return False

    markers = 0
    expire_at = payload.get('expireAt')
    if expire_at is not None:
        parsed_expire_at = _parse_datetime(expire_at)
        if not parsed_expire_at or abs((parsed_expire_at - _as_utc(overlay.expire_at)).total_seconds()) > 2:
            return False
        markers += 1

    traffic_limit = payload.get('trafficLimitBytes')
    if traffic_limit is not None:
        try:
            if int(traffic_limit) != overlay.traffic_limit_bytes:
                return False
        except (TypeError, ValueError):
            return False
        markers += 1

    if 'activeInternalSquads' in payload:
        payload_squads = _extract_squad_uuids(payload.get('activeInternalSquads'))
        if set(payload_squads) != set(overlay.squad_uuids):
            return False
        markers += 1

    return markers > 0


def _extract_squad_uuids(raw_squads: Any) -> tuple[str, ...]:
    if not isinstance(raw_squads, list):
        return ()

    result: list[str] = []
    for squad in raw_squads:
        raw_uuid = squad.get('uuid') if isinstance(squad, dict) else squad
        if raw_uuid is None:
            continue
        squad_uuid = str(raw_uuid)
        if squad_uuid not in result:
            result.append(squad_uuid)
    return tuple(result)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.replace('Z', '+00:00')))
    except ValueError:
        return None


def _is_later(current: datetime | None, previous: datetime | None) -> bool:
    if current is None:
        return False
    if previous is None:
        return True
    return _as_utc(current) > _as_utc(previous)


def _datetimes_equal(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return left is right
    return abs((_as_utc(left) - _as_utc(right)).total_seconds()) <= 1


def _normalize_status(value: object) -> str:
    raw_value = getattr(value, 'value', value)
    return str(raw_value).strip().lower().rsplit('.', maxsplit=1)[-1]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _error_text(error: Exception) -> str:
    return f'{type(error).__name__}: {error}'[:1000]


def _utc_now() -> datetime:
    return datetime.now(UTC)
