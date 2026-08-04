"""Business rules for temporary restricted access after subscription exhaustion.

This module intentionally contains no SQLAlchemy, scheduler, webhook, or Remnawave
SDK wiring.  Those integrations are represented by small protocols so the grace
rules remain testable and so future upstream updates only need a few stable entry
points.

The billing subscription always remains the source of truth.  Grace is a
temporary overlay in Remnawave and is recorded as a separate session.  The one
intentional exception to the otherwise read-only traffic accounting rule is a
tariff switch whose existing billing policy explicitly requested a traffic
reset; Grace coordinates that reset so its temporary quota cannot be inflated.
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

_RESTORE_ECHO_TIMESTAMP_TOLERANCE = timedelta(minutes=1)
_COMPLETED_EXPIRED_ECHO_DEDUPE_TTL = timedelta(minutes=30)


class GraceReason(StrEnum):
    EXPIRED = 'expired'
    LIMITED = 'limited'


class GraceSubscriptionKind(StrEnum):
    """Mutually exclusive subscription category used by grace eligibility."""

    TRIAL = 'trial'
    DAILY = 'daily'
    FREE = 'free'
    REGULAR_PAID = 'regular_paid'


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


class GraceTrafficResetOutcome(StrEnum):
    """Verified result of a tariff-switch reset while Grace is open."""

    RECOVERED = 'recovered'
    CONTINUED = 'continued'
    EXHAUSTED = 'exhausted'


class GracePanelTransitionPending(Exception):
    """Signal that Remnawave is still deriving a server-owned panel status."""


class GracePanelTransitionConflict(Exception):
    """Signal that a derived-status transition hit an unrelated panel state."""


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
    traffic_bytes: int = 1024**3
    trial_enabled: bool = False
    daily_enabled: bool = False
    free_enabled: bool = False
    reconcile_batch_size: int = 200
    reset_traffic_on_tariff_switch: bool = False

    def __post_init__(self) -> None:
        if self.duration <= timedelta(0):
            raise ValueError('Grace duration must be positive')
        if self.traffic_bytes < 0:
            raise ValueError('Grace traffic must not be negative')
        if self.reconcile_batch_size < 1:
            raise ValueError('Grace reconcile batch size must be positive')

    def squad_for(self, reason: GraceReason) -> str:
        squad_uuid = self.expired_squad_uuid if reason is GraceReason.EXPIRED else self.limited_squad_uuid
        if not squad_uuid.strip():
            raise ValueError(f'Grace squad UUID for {reason.value} is required when GRACE_ACCESS_MODE=true')
        return squad_uuid


@dataclass(frozen=True, slots=True)
class GraceBillingState:
    """Canonical subscription data owned by the bot billing database."""

    subscription_id: int
    # Remnawave 3.0.0 identifies a panel user by its numeric ``id`` only.
    remnawave_id: int | None
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
    # ``tariff_id_known`` distinguishes a real ``NULL`` tariff from legacy
    # persisted Grace snapshots created before tariff identity was captured.
    # Unknown legacy identity must never be accepted as proof of a safe switch.
    tariff_id: int | None = None
    tariff_id_known: bool = False


@dataclass(frozen=True, slots=True)
class GracePanelSnapshot:
    """Remnawave values changed by grace and therefore restored on timeout.

    ``used_traffic_bytes`` is captured for calculating a temporary limit, but it
    must never be restored: traffic consumed during grace is real traffic.
    """

    remnawave_id: int
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
    remnawave_id: int
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
    # Canonical LIMITED incident keys encountered while one unchanged Grace
    # grant is rebased across tariffs.  They are dedupe aliases only: the
    # original incident_key and the temporary overlay remain immutable.
    incident_aliases: tuple[str, ...] = ()
    # Mutable dedupe cursor for the most recently observed tariff in this
    # LIMITED reset generation.  It is deliberately separate from
    # ``billing_before``: the latter tracks the active canonical restore point
    # and must not be rewritten after completion merely to advance dedupe.
    limited_lineage_tail: GraceBillingState | None = None
    # A higher/unlimited replacement tariff can make a stale canonical LIMITED
    # row active. Its resulting user.enabled webhook must be allowed to update
    # billing instead of being mistaken for an ordinary Grace overlay echo.
    allow_recovery_enabled_webhook: bool = False
    # Durable intent written before the irreversible Remnawave reset call.  It
    # contains the exact post-switch canonical state so reconciliation can
    # resume after a crash without issuing a second reset.  Completed immediate
    # resets retain it briefly as proof for suppressing a delayed fence webhook.
    traffic_reset_target: GraceBillingState | None = None
    # Remaining bytes from the original one-shot Grace grant, measured from the
    # panel immediately before the reset.  With no target it is retained as an
    # applied fence fingerprint, not a pending reset. ``None`` means neither is
    # present; zero stays distinct because Remnawave's traffic limit 0 means
    # unlimited.
    traffic_reset_remaining_bytes: int | None = None
    # Lower bound of the irreversible reset operation. It survives later Grace
    # completion so delayed reset webhooks are matched to the reset interval,
    # not to the unrelated time at which the Grace session eventually closes.
    traffic_reset_started_at: datetime | None = None
    # Upper bound of the reset operation. It is deliberately independent from
    # Grace completion, which may happen minutes or days later.
    traffic_reset_finished_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GraceTrafficResetResult:
    """Panel state produced by one idempotent tariff-switch traffic reset."""

    outcome: GraceTrafficResetOutcome
    panel: GracePanelSnapshot
    overlay: GracePanelOverlay | None = None


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

    async def list_recent_completed(
        self,
        subscription_id: int,
        *,
        limit: int = 8,
    ) -> Sequence[GraceAccessSession]: ...

    async def create(self, session: GraceAccessSession) -> GraceAccessSession: ...

    async def save(self, session: GraceAccessSession) -> GraceAccessSession: ...

    async def list_open(self, *, limit: int) -> Sequence[GraceAccessSession]: ...


class GracePanelGateway(Protocol):
    """Remnawave adapter implemented in the next integration step."""

    async def read_snapshot(self, remnawave_id: int) -> GracePanelSnapshot | None: ...

    async def apply_overlay(self, remnawave_id: int, overlay: GracePanelOverlay) -> None: ...

    async def restore_snapshot(
        self,
        remnawave_id: int,
        snapshot: GracePanelSnapshot,
        expected_overlay: GracePanelOverlay,
        *,
        force_disable: bool = False,
    ) -> GraceRestoreOutcome: ...

    async def revoke_missing_billing(
        self,
        remnawave_id: int,
        *,
        expected_overlay: GracePanelOverlay,
    ) -> None: ...

    async def apply_billing_state(
        self,
        billing: GraceBillingState,
        *,
        expected_overlay: GracePanelOverlay,
        expected_restored_snapshot: GracePanelSnapshot | None = None,
        require_overlay_source: bool = False,
        expected_last_traffic_reset_at: datetime | None = None,
    ) -> None: ...

    async def prepare_tariff_rebase(
        self,
        billing: GraceBillingState,
        *,
        expected_overlay: GracePanelOverlay,
        expected_last_traffic_reset_at: datetime | None,
    ) -> GracePanelSnapshot | None: ...

    async def apply_tariff_switch_traffic_reset(
        self,
        billing: GraceBillingState,
        *,
        reason: GraceReason,
        expected_overlay: GracePanelOverlay,
        expected_last_traffic_reset_at: datetime | None,
        remaining_grace_bytes: int,
    ) -> GraceTrafficResetResult: ...


class GraceBillingGateway(Protocol):
    """Canonical billing adapter used by the Grace state machine."""

    async def get_subscription(self, subscription_id: int) -> GraceBillingState | None: ...

    async def mark_active_after_traffic_reset(
        self,
        expected: GraceBillingState,
    ) -> GraceBillingState | None: ...


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
        if not billing_is_eligible(billing, reason, self._policy):
            return GraceStartResult(GraceStartDecision.NOT_ELIGIBLE)
        if not billing.remnawave_id:
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

        if reason is GraceReason.EXPIRED:
            completed_after = _as_utc(self._clock()) - _COMPLETED_EXPIRED_ECHO_DEDUPE_TTL
            completed_sessions = await self._store.list_recent_completed(
                billing.subscription_id,
                limit=8,
            )
            for completed_session in completed_sessions:
                if (
                    completed_session.completed_at is not None
                    and _as_utc(completed_session.completed_at) >= completed_after
                    and billing_matches_completed_expired_echo(billing, completed_session)
                ):
                    return GraceStartResult(
                        GraceStartDecision.ALREADY_GRANTED,
                        completed_session,
                    )

        panel_snapshot = await self._panel.read_snapshot(billing.remnawave_id)
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
        lineage_key: str | None = None
        if reason is GraceReason.LIMITED:
            lineage_key = build_tariff_rebase_lineage_key(
                billing,
                reason,
                last_traffic_reset_at=panel_snapshot.last_traffic_reset_at,
            )
            lineage_session = await self._store.get_by_incident(
                billing.subscription_id,
                lineage_key,
            )
            if lineage_session and tariff_rebase_lineage_blocks_new_grant(
                billing,
                lineage_session,
            ):
                lineage_session = await self._advance_blocked_limited_lineage(
                    lineage_session,
                    billing,
                    incident_key=incident_key,
                )
                return GraceStartResult(
                    GraceStartDecision.ALREADY_GRANTED,
                    lineage_session,
                )
            if lineage_session:
                # A proven same-tariff entitlement increase may legitimately
                # reuse a byte limit seen on an older tariff. Give that grant a
                # deterministic tariff-qualified key. Besides avoiding the old
                # key collision, this permits each tariff/quota entitlement only
                # once per reset generation even if tariffs are later cycled.
                incident_key = _build_limited_entitlement_incident_key(
                    incident_key,
                    billing,
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
            remnawave_id=billing.remnawave_id,
            reason=reason,
            incident_key=incident_key,
            state=GraceSessionState.PENDING,
            billing_before=billing,
            panel_before=panel_snapshot,
            overlay=overlay,
            started_at=now,
            grace_until=overlay.expire_at,
            updated_at=now,
            incident_aliases=((lineage_key,) if lineage_key is not None else ()),
            limited_lineage_tail=(billing if reason is GraceReason.LIMITED else None),
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

    async def _advance_blocked_limited_lineage(
        self,
        session: GraceAccessSession,
        billing: GraceBillingState,
        *,
        incident_key: str,
    ) -> GraceAccessSession:
        """Remember a post-completion switch without changing restore history."""
        tail = session.limited_lineage_tail or session.billing_before
        if (
            session.state is not GraceSessionState.COMPLETED
            or session.reason is not GraceReason.LIMITED
            or billing.remnawave_id != tail.remnawave_id
            or not billing.tariff_id_known
        ):
            return session
        if tail.tariff_id_known and billing.tariff_id == tail.tariff_id:
            return session

        aliases = tuple(dict.fromkeys((*session.incident_aliases, incident_key)))
        updated = replace(
            session,
            incident_aliases=aliases,
            limited_lineage_tail=billing,
            updated_at=_as_utc(self._clock()),
        )
        return await self._store.save(updated)

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
            await self._panel.apply_billing_state(
                billing,
                expected_overlay=session.overlay,
            )
        await self._complete(session, GraceCompletionReason.PAID)
        return True

    async def apply_tariff_switch_traffic_reset(self, subscription_id: int) -> str | None:
        """Apply an explicitly configured tariff-switch reset under Grace control.

        ``None`` means the open session is not an exact tariff-switch reset
        candidate.  Callers must never fall back to a direct panel reset while
        Grace remains open: the old absolute overlay limit contains historical
        usage and would turn into extra free quota after the counter is zeroed.
        """
        session = await self._store.get_open(subscription_id)
        if session is None:
            return None
        billing = await self._billing.get_subscription(subscription_id)
        if billing is None:
            return None
        return await self._try_tariff_switch_traffic_reset(
            session,
            billing,
            now=_as_utc(self._clock()),
            force_restore=False,
        )

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
        ``force_restore`` is reserved for the explicit emergency CLI and requests
        an immediate fail-closed restore. A panel-owned LIMITED transition stays
        pending until Remnawave derives EXPIRED because forcing it would require
        an indistinguishable user.disabled event. PENDING sessions are never activated.
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
            except GracePanelTransitionConflict as error:
                latest_session = await self._store.get_open(session.subscription_id)
                if latest_session is None:
                    result = replace(result, unchanged=result.unchanged + 1)
                    continue
                await self._complete(
                    latest_session,
                    GraceCompletionReason.CONFLICT,
                    last_error=_error_text(error),
                    retain_traffic_reset_proof=latest_session.traffic_reset_target is not None,
                )
                result = replace(result, conflicts=result.conflicts + 1)
                continue
            except GracePanelTransitionPending:
                await self._clear_error(session.subscription_id)
                result = replace(result, unchanged=result.unchanged + 1)
                continue
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
        """Return whether a panel event is a provable grace-owned panel echo."""
        normalized_event = event_name.strip().lower()
        session = await self._store.get_open(subscription_id)

        if session is None:
            if normalized_event not in {
                'user.modified',
                'user.enabled',
                'user.traffic_reset',
            }:
                return False
            completed_sessions = await self._store.list_recent_completed(
                subscription_id,
                limit=8,
            )
            if normalized_event == 'user.modified':
                return any(
                    webhook_matches_expired_restore(payload, completed_session)
                    or webhook_matches_traffic_reset_intermediate(
                        payload,
                        completed_session,
                    )
                    for completed_session in completed_sessions
                )
            return any(
                webhook_matches_traffic_reset_signal(payload, completed_session)
                for completed_session in completed_sessions
            )

        # A real administrative disable must always win.  We deliberately let
        # restore-generated ``user.disabled`` pass too: changing EXPIRED to
        # DISABLED is less dangerous than hiding a real revocation and later
        # re-enabling it.
        if normalized_event == 'user.disabled':
            return False

        # Ordinary user.modified events are handled field-by-field by the
        # webhook service.  A strictly matching restore echo is suppressed here
        # to close the race where RESTORING becomes COMPLETED between the guard
        # and that field-level masking query.
        if normalized_event == 'user.modified':
            return session.state is GraceSessionState.RESTORING and webhook_matches_expired_restore(
                payload,
                session,
            )

        if normalized_event == 'user.enabled' and session.allow_recovery_enabled_webhook:
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
            await self._panel.apply_billing_state(
                latest_billing,
                expected_overlay=session.overlay,
            )
            return await self._complete(session, GraceCompletionReason.PAID)

        if latest_billing is None or billing_is_revoked(latest_billing):
            completion_reason = GraceCompletionReason.REVOKED
            action = await self._restore_and_complete(session, completion_reason)
            return action[1]

        if (
            now >= _as_utc(session.grace_until)
            or not billing_incident_is_eligible(latest_billing, session.reason)
            or not billing_still_matches_session(session, latest_billing)
        ):
            action = await self._restore_and_complete(session, GraceCompletionReason.CONFLICT)
            return action[1]

        current_panel = await self._panel.read_snapshot(session.remnawave_id)
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
                await self._panel.apply_billing_state(
                    latest_billing,
                    expected_overlay=session.overlay,
                )
            except (GracePanelTransitionConflict, GracePanelTransitionPending):
                raise
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
                await self._panel.apply_overlay(session.remnawave_id, session.overlay)
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
            await self._panel.apply_billing_state(
                latest_billing,
                expected_overlay=session.overlay,
            )
            return await self._complete(session, GraceCompletionReason.PAID)
        if latest_billing is None or billing_is_revoked(latest_billing):
            if latest_billing is not None:
                await self._panel.apply_billing_state(
                    latest_billing,
                    expected_overlay=session.overlay,
                )
                return await self._complete(session, GraceCompletionReason.REVOKED)
            _, completed = await self._restore_and_complete(session, GraceCompletionReason.REVOKED)
            return completed
        if not billing_incident_is_eligible(latest_billing, session.reason) or not billing_still_matches_session(
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
        if session.traffic_reset_target is not None and billing is not None:
            reset_action = await self._try_tariff_switch_traffic_reset(
                session,
                billing,
                now=_as_utc(self._clock()),
                force_restore=force_restore,
            )
            if reset_action is not None:
                return reset_action

        if billing and billing_has_recovered(session, billing):
            await self._panel.apply_billing_state(
                billing,
                expected_overlay=session.overlay,
            )
            await self._complete(session, GraceCompletionReason.PAID)
            return GraceCompletionReason.PAID.value

        if billing is None or billing_is_revoked(billing):
            if billing is not None:
                await self._panel.apply_billing_state(
                    billing,
                    expected_overlay=session.overlay,
                )
                latest_billing = await self._billing.get_subscription(session.subscription_id)
                if latest_billing and billing_has_recovered(session, latest_billing):
                    await self._panel.apply_billing_state(
                        latest_billing,
                        expected_overlay=session.overlay,
                    )
                    await self._complete(session, GraceCompletionReason.PAID)
                    return GraceCompletionReason.PAID.value
                await self._complete(session, GraceCompletionReason.REVOKED)
                return GraceCompletionReason.REVOKED.value
            action, _ = await self._restore_and_complete(session, GraceCompletionReason.REVOKED)
            return action

        now = _as_utc(self._clock())

        # A proven tariff switch may update the future restore point while the
        # already granted restricted overlay keeps its original quota and
        # deadline.  Every ambiguous canonical or panel change still follows
        # the fail-closed conflict path below.
        if not billing_incident_is_eligible(billing, session.reason) or not billing_still_matches_session(
            session, billing
        ):
            reset_action = await self._try_tariff_switch_traffic_reset(
                session,
                billing,
                now=now,
                force_restore=force_restore,
            )
            if reset_action is not None:
                return reset_action

            rebased_action = await self._try_rebase_tariff_change(
                session,
                billing,
                now=now,
                force_restore=force_restore,
            )
            if rebased_action is not None:
                return rebased_action

            # The recipient or canonical incident changed while grace was open
            # (admin cancellation/shortening, panel id replacement, or an
            # unprovable squads/device/limit change). Canonical billing wins for
            # the same panel user; unrelated panel changes remain untouched by
            # the gateway compare-and-set checks.
            # ``session.remnawave_id`` is always a positive int, so a
            # subscription that lost its panel link cannot match by accident.
            if billing.remnawave_id == session.remnawave_id:
                session = await self._remember_terminal_tariff_lineage(session, billing)
                if session.state is GraceSessionState.COMPLETED:
                    return (
                        session.completion_reason or GraceCompletionReason.CONFLICT
                    ).value
                await self._panel.apply_billing_state(
                    billing,
                    expected_overlay=session.overlay,
                    expected_restored_snapshot=(
                        session.panel_before
                        if session.state is GraceSessionState.RESTORING
                        else None
                    ),
                )
                await self._complete(session, GraceCompletionReason.CONFLICT)
                return GraceCompletionReason.CONFLICT.value
            action, _ = await self._restore_and_complete(session, GraceCompletionReason.CONFLICT)
            return action

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
            current_panel = await self._panel.read_snapshot(session.remnawave_id)
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
                await self._panel.apply_billing_state(
                    billing,
                    expected_overlay=session.overlay,
                )
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

    async def _try_tariff_switch_traffic_reset(
        self,
        session: GraceAccessSession,
        billing: GraceBillingState,
        *,
        now: datetime,
        force_restore: bool,
    ) -> str | None:
        target = session.traffic_reset_target
        remaining_bytes = session.traffic_reset_remaining_bytes

        if target is None:
            if not tariff_change_requires_traffic_reset(
                session,
                billing,
                self._policy,
                now=now,
                force_restore=force_restore,
            ):
                return None

            current_panel = await self._panel.read_snapshot(session.remnawave_id)
            if current_panel is None or not current_panel.traffic_is_known:
                raise GracePanelTransitionConflict(
                    'Remnawave traffic state is unavailable before the configured tariff reset'
                )
            overlay_matches = panel_matches_overlay(
                current_panel,
                session.overlay,
                now=now,
            )
            reset_generation_matches = _reset_generations_equal(
                current_panel.last_traffic_reset_at,
                session.panel_before.last_traffic_reset_at,
            )
            if not overlay_matches or not reset_generation_matches:
                raise GracePanelTransitionConflict(
                    'Remnawave changed before the configured tariff reset could be checkpointed'
                )

            remaining_bytes = max(
                0,
                session.overlay.traffic_limit_bytes - current_panel.used_traffic_bytes,
            )
            checkpoint = replace(
                session,
                traffic_reset_target=billing,
                traffic_reset_remaining_bytes=remaining_bytes,
                traffic_reset_started_at=now,
                traffic_reset_finished_at=None,
                allow_recovery_enabled_webhook=session.reason is GraceReason.LIMITED,
                updated_at=now,
                last_error=None,
            )
            session = await self._store.save(checkpoint)
            if session.state is GraceSessionState.COMPLETED:
                return (session.completion_reason or GraceCompletionReason.CONFLICT).value
            target = session.traffic_reset_target
            remaining_bytes = session.traffic_reset_remaining_bytes

        if target is None or remaining_bytes is None:
            raise GracePanelTransitionConflict('Persisted tariff reset intent is incomplete')

        fresh_billing = await self._billing.get_subscription(session.subscription_id)
        if fresh_billing is None:
            return await self._finish_changed_traffic_reset_checkpoint(
                session,
                None,
                checkpoint_target=target,
                remaining_bytes=remaining_bytes,
                now=now,
            )
        if not traffic_reset_billing_matches_target(fresh_billing, target, session.reason):
            return await self._finish_changed_traffic_reset_checkpoint(
                session,
                fresh_billing,
                checkpoint_target=target,
                remaining_bytes=remaining_bytes,
                now=now,
            )

        reset_result = await self._panel.apply_tariff_switch_traffic_reset(
            target,
            reason=session.reason,
            expected_overlay=session.overlay,
            expected_last_traffic_reset_at=session.panel_before.last_traffic_reset_at,
            remaining_grace_bytes=remaining_bytes,
        )
        reset_finished_at = _bounded_traffic_reset_finished_at(
            session,
            observed_at=reset_result.panel.last_traffic_reset_at,
            now=_as_utc(self._clock()),
        )
        session = await self._store.save(
            replace(
                session,
                traffic_reset_started_at=(
                    session.traffic_reset_started_at or session.updated_at
                ),
                traffic_reset_finished_at=reset_finished_at,
                updated_at=_as_utc(self._clock()),
            )
        )
        if session.state is GraceSessionState.COMPLETED:
            return (session.completion_reason or GraceCompletionReason.CONFLICT).value

        latest_billing = await self._billing.get_subscription(session.subscription_id)
        if latest_billing is None or not traffic_reset_billing_matches_target(
            latest_billing,
            target,
            session.reason,
        ):
            return await self._finish_changed_traffic_reset_checkpoint(
                session,
                latest_billing,
                checkpoint_target=target,
                remaining_bytes=remaining_bytes,
                now=_as_utc(self._clock()),
            )

        current_incident_key = build_incident_key(
            target,
            session.reason,
            last_traffic_reset_at=reset_result.panel.last_traffic_reset_at,
        )
        lineage_key = build_tariff_rebase_lineage_key(
            target,
            session.reason,
            last_traffic_reset_at=reset_result.panel.last_traffic_reset_at,
        )
        aliases = tuple(
            value
            for value in dict.fromkeys(
                (*session.incident_aliases, current_incident_key, lineage_key)
            )
            if value != session.incident_key
        )

        if reset_result.outcome is GraceTrafficResetOutcome.RECOVERED:
            active_billing = await self._billing.mark_active_after_traffic_reset(target)
            if active_billing is None or _normalize_status(active_billing.status) != 'active':
                raise GracePanelTransitionConflict(
                    'Canonical LIMITED subscription was not activated after its verified traffic reset'
                )
            completed_source = replace(
                session,
                billing_before=target,
                incident_aliases=aliases,
                limited_lineage_tail=target,
                updated_at=_as_utc(self._clock()),
                last_error=None,
            )
            await self._complete(
                completed_source,
                GraceCompletionReason.PAID,
                retain_traffic_reset_proof=True,
            )
            return GraceCompletionReason.PAID.value

        if reset_result.outcome is GraceTrafficResetOutcome.EXHAUSTED:
            exhausted_source = replace(
                session,
                billing_before=target,
                incident_aliases=aliases,
                allow_recovery_enabled_webhook=False,
                updated_at=_as_utc(self._clock()),
                last_error=None,
            )
            await self._complete(
                exhausted_source,
                GraceCompletionReason.TIMEOUT,
                retain_traffic_reset_proof=True,
            )
            return GraceCompletionReason.TIMEOUT.value

        if reset_result.overlay is None:
            raise GracePanelTransitionConflict(
                'Remnawave did not return the continued Grace overlay after traffic reset'
            )
        rebased_panel = replace(
            session.panel_before,
            expire_at=target.end_at,
            traffic_limit_bytes=target.traffic_limit_bytes,
            used_traffic_bytes=reset_result.panel.used_traffic_bytes,
            squad_uuids=target.squad_uuids,
            external_squad_uuid=target.external_squad_uuid,
            last_traffic_reset_at=reset_result.panel.last_traffic_reset_at,
        )
        continued = replace(
            session,
            billing_before=target,
            panel_before=rebased_panel,
            overlay=reset_result.overlay,
            incident_aliases=aliases,
            limited_lineage_tail=(target if session.reason is GraceReason.LIMITED else None),
            allow_recovery_enabled_webhook=False,
            traffic_reset_target=None,
            # The pending intent is cleared, while the applied fence quota is
            # retained as a delayed reset-webhook fingerprint.
            traffic_reset_remaining_bytes=remaining_bytes,
            updated_at=_as_utc(self._clock()),
            last_error=None,
        )
        saved = await self._store.save(continued)
        if saved.state is GraceSessionState.COMPLETED:
            return (saved.completion_reason or GraceCompletionReason.CONFLICT).value
        return 'repaired'

    async def _finish_changed_traffic_reset_checkpoint(
        self,
        session: GraceAccessSession,
        billing: GraceBillingState | None,
        *,
        checkpoint_target: GraceBillingState,
        remaining_bytes: int,
        now: datetime,
    ) -> str:
        """Resolve a superseding canonical change from an exact reset state.

        The reset marker may already have produced either the original overlay,
        its quota fence, or the verified post-reset target.  Closing the session
        without first converging one of those states would strand temporary
        Grace routing after the session is no longer repairable.
        """
        current_panel = await self._panel.read_snapshot(session.remnawave_id)
        if current_panel is None:
            if billing is None:
                await self._complete(
                    session,
                    GraceCompletionReason.REVOKED,
                    retain_traffic_reset_proof=True,
                )
                return GraceCompletionReason.REVOKED.value
            raise GracePanelTransitionConflict(
                'Remnawave user disappeared while the tariff reset target was changing'
            )
        expected_source = traffic_reset_checkpoint_source_overlay(
            session,
            current_panel,
            checkpoint_target=checkpoint_target,
            remaining_bytes=remaining_bytes,
            now=now,
        )
        if expected_source is None:
            raise GracePanelTransitionConflict(
                'Remnawave changed outside the persisted tariff reset checkpoint'
            )

        reset_generation_changed = not _reset_generations_equal(
            current_panel.last_traffic_reset_at,
            session.panel_before.last_traffic_reset_at,
        )
        if reset_generation_changed and session.traffic_reset_finished_at is None:
            session = await self._store.save(
                replace(
                    session,
                    traffic_reset_finished_at=_bounded_traffic_reset_finished_at(
                        session,
                        observed_at=current_panel.last_traffic_reset_at,
                        now=now,
                    ),
                    updated_at=now,
                )
            )
            if session.state is GraceSessionState.COMPLETED:
                return (session.completion_reason or GraceCompletionReason.CONFLICT).value

        if billing is None:
            await self._panel.revoke_missing_billing(
                session.remnawave_id,
                expected_overlay=expected_source,
            )
            await self._complete(
                session,
                GraceCompletionReason.REVOKED,
                retain_traffic_reset_proof=True,
            )
            return GraceCompletionReason.REVOKED.value

        if (
            session.reason is GraceReason.LIMITED
            and reset_generation_changed
            and _normalize_status(billing.status) == 'limited'
            and billing.used_traffic_bytes == 0
            and billing.end_at is not None
            and _as_utc(billing.end_at) > now
        ):
            active_target = replace(billing, status='active')
            await self._panel.apply_billing_state(
                active_target,
                expected_overlay=expected_source,
                require_overlay_source=_normalize_status(current_panel.status) in {'active', 'limited'},
                expected_last_traffic_reset_at=current_panel.last_traffic_reset_at,
            )
            active_billing = await self._billing.mark_active_after_traffic_reset(billing)
            if active_billing is None or _normalize_status(active_billing.status) != 'active':
                raise GracePanelTransitionConflict(
                    'Superseding LIMITED billing was not activated after the verified reset'
                )
            completed_source = replace(
                session,
                billing_before=billing,
                updated_at=_as_utc(self._clock()),
                last_error=None,
            )
            await self._complete(
                completed_source,
                GraceCompletionReason.PAID,
                retain_traffic_reset_proof=True,
            )
            return GraceCompletionReason.PAID.value

        completion_reason = GraceCompletionReason.CONFLICT
        if billing_has_recovered(session, billing):
            completion_reason = GraceCompletionReason.PAID
        elif billing_is_revoked(billing):
            completion_reason = GraceCompletionReason.REVOKED

        await self._panel.apply_billing_state(
            billing,
            expected_overlay=expected_source,
            require_overlay_source=(
                _normalize_status(billing.status) in {'active', 'trial'}
                and _normalize_status(current_panel.status) in {'active', 'limited'}
            ),
            expected_last_traffic_reset_at=current_panel.last_traffic_reset_at,
        )
        completed_source = replace(
            session,
            billing_before=billing,
            updated_at=_as_utc(self._clock()),
            last_error=(
                'Canonical billing superseded the persisted tariff reset target'
                if completion_reason is GraceCompletionReason.CONFLICT
                else None
            ),
        )
        await self._complete(
            completed_source,
            completion_reason,
            last_error=completed_source.last_error,
            retain_traffic_reset_proof=True,
        )
        return completion_reason.value

    async def _try_rebase_tariff_change(
        self,
        session: GraceAccessSession,
        billing: GraceBillingState,
        *,
        now: datetime,
        force_restore: bool,
        recovery_checkpointed: bool = False,
    ) -> str | None:
        if not tariff_change_can_preserve_grace(
            session,
            billing,
            self._policy,
            now=now,
            force_restore=force_restore,
        ):
            return None

        current_panel = await self._panel.prepare_tariff_rebase(
            billing,
            expected_overlay=session.overlay,
            expected_last_traffic_reset_at=session.panel_before.last_traffic_reset_at,
        )
        if current_panel is None:
            return None

        if session.reason is GraceReason.LIMITED:
            if not current_panel.traffic_is_known:
                return None
            subscription_is_unexpired = bool(
                billing.end_at is not None and _as_utc(billing.end_at) > now
            )
            new_tariff_has_access = (
                billing.traffic_limit_bytes == 0
                or current_panel.used_traffic_bytes < billing.traffic_limit_bytes
            )
            if subscription_is_unexpired and new_tariff_has_access:
                # The database status can remain LIMITED until Remnawave echoes
                # the tariff PATCH.  The open Grace guard deliberately masks that
                # PATCH, so derive the factual recovery from fresh panel traffic
                # and let canonical access win without waiting for the webhook.
                if not recovery_checkpointed:
                    recovering_session = replace(
                        session,
                        allow_recovery_enabled_webhook=True,
                        updated_at=now,
                        last_error=None,
                    )
                    recovering_session = await self._store.save(recovering_session)
                    if recovering_session.state is GraceSessionState.COMPLETED:
                        return (
                            recovering_session.completion_reason
                            or GraceCompletionReason.CONFLICT
                        ).value

                    # Saving the webhook marker is an intentional durable
                    # checkpoint and therefore releases/reacquires the database
                    # lock. Discard every pre-checkpoint billing decision and
                    # process only the winner observed under the new lock.
                    fresh_billing = await self._billing.get_subscription(session.subscription_id)
                    checkpoint_session = replace(
                        recovering_session,
                        allow_recovery_enabled_webhook=False,
                    )
                    if fresh_billing is not None:
                        fresh_action = await self._try_rebase_tariff_change(
                            checkpoint_session,
                            fresh_billing,
                            now=_as_utc(self._clock()),
                            force_restore=False,
                            recovery_checkpointed=True,
                        )
                        if fresh_action is not None:
                            return fresh_action
                        fresh_completion = await self._complete_for_fresh_billing_change(
                            checkpoint_session,
                            fresh_billing,
                            expected_restored_snapshot=None,
                        )
                        if fresh_completion is not None:
                            return fresh_completion[0]
                        if billing_still_matches_session(checkpoint_session, fresh_billing):
                            await self._store.save(
                                replace(
                                    checkpoint_session,
                                    updated_at=_as_utc(self._clock()),
                                    last_error=None,
                                )
                            )
                            return 'unchanged'

                    completion_reason = (
                        GraceCompletionReason.REVOKED
                        if fresh_billing is None
                        else GraceCompletionReason.CONFLICT
                    )
                    action, _ = await self._restore_and_complete(
                        checkpoint_session,
                        completion_reason,
                    )
                    return action

                recovered_billing = replace(
                    billing,
                    status='active',
                    used_traffic_bytes=current_panel.used_traffic_bytes,
                )
                await self._panel.apply_billing_state(
                    recovered_billing,
                    expected_overlay=session.overlay,
                    require_overlay_source=True,
                    expected_last_traffic_reset_at=session.panel_before.last_traffic_reset_at,
                )
                await self._complete(session, GraceCompletionReason.PAID)
                return GraceCompletionReason.PAID.value

        current_incident_key = build_incident_key(
            billing,
            session.reason,
            last_traffic_reset_at=current_panel.last_traffic_reset_at,
        )
        lineage_key = build_tariff_rebase_lineage_key(
            billing,
            session.reason,
            last_traffic_reset_at=current_panel.last_traffic_reset_at,
        )
        aliases = tuple(
            dict.fromkeys(
                (
                    *session.incident_aliases,
                    current_incident_key,
                    lineage_key,
                )
            )
        )
        aliases = tuple(value for value in aliases if value != session.incident_key)
        rebased_panel = replace(
            session.panel_before,
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            used_traffic_bytes=current_panel.used_traffic_bytes,
            squad_uuids=billing.squad_uuids,
            external_squad_uuid=billing.external_squad_uuid,
        )
        rebased = replace(
            session,
            billing_before=billing,
            panel_before=rebased_panel,
            incident_aliases=aliases,
            limited_lineage_tail=billing,
            allow_recovery_enabled_webhook=False,
            updated_at=now,
            last_error=None,
        )
        saved = await self._store.save(rebased)
        if saved.state is GraceSessionState.COMPLETED:
            return (saved.completion_reason or GraceCompletionReason.CONFLICT).value

        logger.info(
            'Grace canonical tariff restore point rebased',
            subscription_id=session.subscription_id,
            grace_session_id=session.id,
            reason=session.reason.value,
            old_tariff_id=session.billing_before.tariff_id,
            new_tariff_id=billing.tariff_id,
            grace_until=session.grace_until,
        )
        return 'repaired'

    async def _complete_for_fresh_billing_change(
        self,
        session: GraceAccessSession,
        billing: GraceBillingState | None,
        *,
        expected_restored_snapshot: GracePanelSnapshot | None,
    ) -> tuple[str, GraceAccessSession] | None:
        if billing is None:
            return None

        if billing_has_recovered(session, billing):
            await self._panel.apply_billing_state(
                billing,
                expected_overlay=session.overlay,
                expected_restored_snapshot=expected_restored_snapshot,
            )
            completed = await self._complete(session, GraceCompletionReason.PAID)
            return GraceCompletionReason.PAID.value, completed

        if billing_is_revoked(billing):
            await self._panel.apply_billing_state(
                billing,
                expected_overlay=session.overlay,
                expected_restored_snapshot=expected_restored_snapshot,
            )
            completed = await self._complete(session, GraceCompletionReason.REVOKED)
            return GraceCompletionReason.REVOKED.value, completed

        billing_changed = not billing_incident_is_eligible(
            billing,
            session.reason,
        ) or not billing_still_matches_session(session, billing)
        if not billing_changed or billing.remnawave_id != session.remnawave_id:
            return None

        session = await self._remember_terminal_tariff_lineage(session, billing)
        if session.state is GraceSessionState.COMPLETED:
            reason = session.completion_reason or GraceCompletionReason.CONFLICT
            return reason.value, session
        await self._panel.apply_billing_state(
            billing,
            expected_overlay=session.overlay,
            expected_restored_snapshot=expected_restored_snapshot,
        )
        completed = await self._complete(session, GraceCompletionReason.CONFLICT)
        return GraceCompletionReason.CONFLICT.value, completed

    async def _remember_terminal_tariff_lineage(
        self,
        session: GraceAccessSession,
        billing: GraceBillingState,
    ) -> GraceAccessSession:
        if (
            session.reason is not GraceReason.LIMITED
            or not tariff_change_matches_incident_family(session, billing, self._policy)
        ):
            return session
        current_incident_key = build_incident_key(
            billing,
            session.reason,
            last_traffic_reset_at=session.panel_before.last_traffic_reset_at,
        )
        lineage_key = build_tariff_rebase_lineage_key(
            billing,
            session.reason,
            last_traffic_reset_at=session.panel_before.last_traffic_reset_at,
        )
        aliases = tuple(
            dict.fromkeys(
                (*session.incident_aliases, current_incident_key, lineage_key)
            )
        )
        updated = replace(
            session,
            incident_aliases=aliases,
            limited_lineage_tail=billing,
            allow_recovery_enabled_webhook=False,
            updated_at=_as_utc(self._clock()),
        )
        if session.state is GraceSessionState.RESTORING:
            # Persist this metadata together with the terminal CAS below. A
            # RESTORING -> RESTORING save is itself a durable checkpoint; doing
            # it here would release the billing lock between the fresh read and
            # its canonical panel PATCH.
            return updated
        return await self._store.save(updated)

    async def _restore_and_complete(
        self,
        session: GraceAccessSession,
        completion_reason: GraceCompletionReason,
    ) -> tuple[str, GraceAccessSession]:
        now = _as_utc(self._clock())
        # Refresh the durable checkpoint before every external restore attempt.
        # Besides crash recovery, this timestamps the narrow window in which a
        # full user.modified payload can be proven to be our own restore echo.
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
        fresh_completion = await self._complete_for_fresh_billing_change(
            restoring_session,
            latest_billing,
            expected_restored_snapshot=None,
        )
        if fresh_completion is not None:
            return fresh_completion

        outcome = await self._panel.restore_snapshot(
            restoring_session.remnawave_id,
            restoring_session.panel_before,
            restoring_session.overlay,
            force_disable=completion_reason is not GraceCompletionReason.TIMEOUT,
        )

        # Payment may land after the pre-restore check.  Paid billing always wins
        # over an old snapshot, even if the restore PATCH has already succeeded.
        latest_billing = await self._billing.get_subscription(session.subscription_id)
        fresh_completion = await self._complete_for_fresh_billing_change(
            restoring_session,
            latest_billing,
            expected_restored_snapshot=restoring_session.panel_before,
        )
        if fresh_completion is not None:
            return fresh_completion

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
        retain_traffic_reset_proof: bool = False,
    ) -> GraceAccessSession:
        now = _as_utc(self._clock())
        has_applied_fence_proof = (
            session.traffic_reset_target is None
            and session.traffic_reset_remaining_bytes is not None
        )
        retain_reset_proof = retain_traffic_reset_proof or has_applied_fence_proof
        completed_session = replace(
            session,
            state=GraceSessionState.COMPLETED,
            completion_reason=completion_reason,
            completed_at=now,
            updated_at=now,
            last_error=last_error,
            allow_recovery_enabled_webhook=False,
            traffic_reset_target=(session.traffic_reset_target if retain_reset_proof else None),
            traffic_reset_remaining_bytes=(
                session.traffic_reset_remaining_bytes if retain_reset_proof else None
            ),
            traffic_reset_started_at=(
                (session.traffic_reset_started_at or session.updated_at)
                if retain_reset_proof
                else None
            ),
            traffic_reset_finished_at=(
                session.traffic_reset_finished_at
                if retain_reset_proof
                else None
            ),
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

    async def _clear_error(self, subscription_id: int) -> None:
        session = await self._store.get_open(subscription_id)
        if not session or session.last_error is None:
            return
        await self._store.save(
            replace(
                session,
                updated_at=_as_utc(self._clock()),
                last_error=None,
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


def build_tariff_rebase_lineage_key(
    billing: GraceBillingState,
    reason: GraceReason,
    *,
    last_traffic_reset_at: datetime | None = None,
) -> str:
    """Identify one tariff-switch lineage without changing grant semantics.

    Ordinary LIMITED incident keys keep the traffic limit so a real traffic
    purchase may earn a later Grace grant.  This additional alias groups only
    tariff-rebased sessions by the underlying traffic-reset generation and is
    used to prevent tariff cycling from minting fresh grants.
    """
    if reason is GraceReason.EXPIRED:
        end_at = _as_utc(billing.end_at).isoformat() if billing.end_at else 'none'
        return f'tariff-rebase:{reason.value}:{end_at}'
    end_at = _as_utc(billing.end_at).isoformat() if billing.end_at else 'none'
    reset_at = _as_utc(last_traffic_reset_at).isoformat() if last_traffic_reset_at else 'unknown'
    return f'tariff-rebase:{reason.value}:{end_at}:{reset_at}'


def tariff_rebase_lineage_blocks_new_grant(
    current: GraceBillingState,
    previous: GraceAccessSession,
) -> bool:
    """Block tariff-derived LIMITED repeats while preserving traffic purchases."""
    before = previous.limited_lineage_tail or previous.billing_before
    if previous.reason is not GraceReason.LIMITED:
        return False
    if previous.completion_reason is GraceCompletionReason.PAID:
        return False
    if current.remnawave_id != previous.remnawave_id:
        return False
    if not current.tariff_id_known or not before.tariff_id_known:
        return True
    if current.tariff_id != before.tariff_id:
        return True
    # On the same tariff, only a strictly larger canonical quota represents a
    # possible new traffic entitlement. Zero is Remnawave's unlimited value,
    # so it compares above every finite quota rather than below it.
    current_limit = current.traffic_limit_bytes
    previous_limit = before.traffic_limit_bytes
    strictly_larger = (
        (current_limit == 0 and previous_limit != 0)
        or (current_limit != 0 and previous_limit != 0 and current_limit > previous_limit)
    )
    return not strictly_larger


def _build_limited_entitlement_incident_key(
    base_incident_key: str,
    billing: GraceBillingState,
) -> str:
    """Disambiguate a lineage-approved grant from an older tariff's quota."""
    if not billing.tariff_id_known:
        tariff_identity = 'unknown'
    elif billing.tariff_id is None:
        tariff_identity = 'none'
    else:
        tariff_identity = str(billing.tariff_id)
    return f'{base_incident_key}:entitlement:{tariff_identity}'


def tariff_change_can_preserve_grace(
    session: GraceAccessSession,
    current: GraceBillingState,
    policy: GraceAccessPolicy,
    *,
    now: datetime,
    force_restore: bool,
) -> bool:
    """Prove that a canonical difference is a non-revoking tariff switch."""
    if force_restore or session.state is not GraceSessionState.ACTIVE:
        return False
    if _as_utc(now) >= _as_utc(session.grace_until):
        return False
    return tariff_change_matches_incident_family(session, current, policy)


def tariff_change_requires_traffic_reset(
    session: GraceAccessSession,
    current: GraceBillingState,
    policy: GraceAccessPolicy,
    *,
    now: datetime,
    force_restore: bool,
) -> bool:
    """Recognize only the DB state produced by the configured tariff reset.

    Tariff switch entry points commit ``used_traffic_bytes == 0`` before their
    panel synchronization call.  The explicit policy bit plus the exact tariff,
    panel id, end-date and subscription-kind proof lets the worker safely recognize
    the same intent if it wins the small race before ``SubscriptionService``.
    """
    if (
        not policy.reset_traffic_on_tariff_switch
        or force_restore
        or session.state is not GraceSessionState.ACTIVE
        or _as_utc(now) >= _as_utc(session.grace_until)
        or current.used_traffic_bytes != 0
    ):
        return False
    before = session.billing_before
    return (
        current.remnawave_id == session.remnawave_id
        and before.tariff_id_known
        and current.tariff_id_known
        and before.tariff_id is not None
        and current.tariff_id is not None
        and before.tariff_id != current.tariff_id
        and before.end_at is not None
        and current.end_at is not None
        and _datetimes_equal(before.end_at, current.end_at)
        and billing_incident_is_eligible(current, session.reason)
        and policy_allows_subscription(current, policy)
        and classify_subscription_kind(current) is classify_subscription_kind(before)
    )


def traffic_reset_billing_matches_target(
    current: GraceBillingState,
    target: GraceBillingState,
    reason: GraceReason,
) -> bool:
    """Keep a persisted reset intent bound to one exact canonical tariff."""
    allowed_statuses = {reason.value}
    if reason is GraceReason.LIMITED:
        # The verified reset emits user.enabled; accepting ACTIVE makes the
        # transition robust whether that webhook wins before or after retry.
        allowed_statuses.add('active')
    return (
        current.subscription_id == target.subscription_id
        and current.remnawave_id == target.remnawave_id
        and _normalize_status(current.status) in allowed_statuses
        and _normalize_status(current.user_status) == _normalize_status(target.user_status) == 'active'
        and _datetimes_equal(current.end_at, target.end_at)
        and current.traffic_limit_bytes == target.traffic_limit_bytes
        and current.used_traffic_bytes >= 0
        and current.device_limit == target.device_limit
        and set(current.squad_uuids) == set(target.squad_uuids)
        and current.external_squad_uuid == target.external_squad_uuid
        and current.is_trial == target.is_trial
        and current.is_daily == target.is_daily
        and current.is_free_tariff == target.is_free_tariff
        and current.tariff_id_known
        and target.tariff_id_known
        and current.tariff_id == target.tariff_id
    )


def tariff_change_matches_incident_family(
    session: GraceAccessSession,
    current: GraceBillingState,
    policy: GraceAccessPolicy,
) -> bool:
    """Prove a tariff-only canonical change independently of session timing."""
    if current.remnawave_id != session.remnawave_id:
        return False
    before = session.billing_before
    if (
        not before.tariff_id_known
        or not current.tariff_id_known
        or before.tariff_id is None
        or current.tariff_id is None
        or before.tariff_id == current.tariff_id
    ):
        return False
    if before.end_at is None or current.end_at is None:
        return False
    if not billing_incident_is_eligible(current, session.reason):
        return False
    if not policy_allows_subscription(current, policy):
        return False
    if classify_subscription_kind(current) is not classify_subscription_kind(before):
        return False
    if current.used_traffic_bytes < before.used_traffic_bytes:
        return False
    return _datetimes_equal(current.end_at, before.end_at)


def billing_matches_completed_expired_echo(
    billing: GraceBillingState,
    session: GraceAccessSession,
) -> bool:
    """Recognize the overlay deadline echoed back as a second expiry incident."""
    billing_before = session.billing_before
    return (
        _normalize_status(billing.status) == GraceReason.EXPIRED.value
        and billing.remnawave_id == session.remnawave_id
        and _session_can_match_expired_restore(session)
        and session.state is GraceSessionState.COMPLETED
        and session.completion_reason
        in {
            GraceCompletionReason.TIMEOUT,
            GraceCompletionReason.DRAINED,
        }
        and _datetimes_equal(billing.end_at, session.overlay.expire_at)
        and billing.traffic_limit_bytes == billing_before.traffic_limit_bytes
        and billing.device_limit == billing_before.device_limit
        and set(billing.squad_uuids) == set(billing_before.squad_uuids)
        and billing.external_squad_uuid == billing_before.external_squad_uuid
        and billing.is_trial == billing_before.is_trial
        and billing.is_daily == billing_before.is_daily
        and billing.is_free_tariff == billing_before.is_free_tariff
        and _normalize_status(billing.user_status) == _normalize_status(billing_before.user_status)
    )


def billing_still_matches_session(
    session: GraceAccessSession,
    current: GraceBillingState,
) -> bool:
    """Compare canonical fields that identify the incident without panel metadata."""
    before = session.billing_before
    if current.remnawave_id != session.remnawave_id:
        return False
    if _normalize_status(current.status) != session.reason.value:
        return False
    if not _datetimes_equal(current.end_at, before.end_at):
        return False
    tariff_matches = (
        not before.tariff_id_known
        or not current.tariff_id_known
        or current.tariff_id == before.tariff_id
    )
    return (
        current.traffic_limit_bytes == before.traffic_limit_bytes
        and current.device_limit == before.device_limit
        and set(current.squad_uuids) == set(before.squad_uuids)
        and current.external_squad_uuid == before.external_squad_uuid
        and current.is_trial == before.is_trial
        and current.is_daily == before.is_daily
        and current.is_free_tariff == before.is_free_tariff
        and tariff_matches
    )


def classify_subscription_kind(billing: GraceBillingState) -> GraceSubscriptionKind:
    """Classify once, in priority order, so overlapping flags are unambiguous."""
    if billing.is_trial:
        return GraceSubscriptionKind.TRIAL
    if billing.is_daily:
        return GraceSubscriptionKind.DAILY
    if billing.is_free_tariff:
        return GraceSubscriptionKind.FREE
    return GraceSubscriptionKind.REGULAR_PAID


def policy_allows_subscription(billing: GraceBillingState, policy: GraceAccessPolicy) -> bool:
    kind = classify_subscription_kind(billing)
    if kind is GraceSubscriptionKind.TRIAL:
        return policy.trial_enabled
    if kind is GraceSubscriptionKind.DAILY:
        return policy.daily_enabled
    if kind is GraceSubscriptionKind.FREE:
        return policy.free_enabled
    return True


def billing_incident_is_eligible(billing: GraceBillingState, reason: GraceReason) -> bool:
    """Check current incident safety without applying new-issuance feature flags."""
    suppressed = False
    if billing.grace_suppressed_until is not None:
        suppressed_until = _as_utc(billing.grace_suppressed_until)
        suppressed = billing.end_at is None or _as_utc(billing.end_at) <= suppressed_until
    return (
        _normalize_status(billing.status) == reason.value
        and _normalize_status(billing.user_status) == 'active'
        and not suppressed
    )


def billing_is_eligible(
    billing: GraceBillingState,
    reason: GraceReason,
    policy: GraceAccessPolicy,
) -> bool:
    """Apply incident safety and subscription-kind flags to a new grant."""
    return billing_incident_is_eligible(billing, reason) and policy_allows_subscription(billing, policy)


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
    if not snapshot.traffic_is_known:
        raise ValueError(f'Remnawave did not return traffic usage for a {reason.value.upper()} user')

    # Remnawave compares its cumulative usage counter with trafficLimitBytes.
    # Keeping the counter and adding the configured grant therefore gives the
    # user exactly ``traffic_bytes`` of usable grace traffic, regardless of the
    # old remaining limit or an old unlimited (zero) limit.
    temporary_limit = snapshot.used_traffic_bytes + policy.traffic_bytes

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


def traffic_reset_checkpoint_source_overlay(
    session: GraceAccessSession,
    current: GracePanelSnapshot,
    *,
    checkpoint_target: GraceBillingState,
    remaining_bytes: int,
    now: datetime,
) -> GracePanelOverlay | None:
    """Prove one panel state produced by the persisted reset checkpoint."""
    fence = replace(
        session.overlay,
        traffic_limit_bytes=max(1, remaining_bytes),
    )
    for candidate in (session.overlay, fence):
        if panel_matches_overlay(current, candidate, now=now):
            return candidate

    reset_generation_changed = not _reset_generations_equal(
        current.last_traffic_reset_at,
        session.panel_before.last_traffic_reset_at,
    )
    if not reset_generation_changed:
        return None

    normalized_status = _normalize_status(current.status)
    if (
        session.reason is GraceReason.LIMITED
        and checkpoint_target.end_at is not None
        and _as_utc(checkpoint_target.end_at) > _as_utc(now)
        and normalized_status == 'active'
        and _datetimes_equal(current.expire_at, checkpoint_target.end_at)
        and current.traffic_limit_bytes == checkpoint_target.traffic_limit_bytes
        and set(current.squad_uuids) == set(checkpoint_target.squad_uuids)
        and current.external_squad_uuid == checkpoint_target.external_squad_uuid
    ):
        return GracePanelOverlay(
            status='ACTIVE',
            expire_at=_as_utc(checkpoint_target.end_at),
            traffic_limit_bytes=checkpoint_target.traffic_limit_bytes,
            squad_uuids=checkpoint_target.squad_uuids,
            external_squad_uuid=checkpoint_target.external_squad_uuid,
        )

    if (
        remaining_bytes == 0
        and normalized_status in {'disabled', 'expired'}
        and _datetimes_equal(current.expire_at, session.overlay.expire_at)
        and current.traffic_limit_bytes == checkpoint_target.traffic_limit_bytes
        and set(current.squad_uuids) == set(checkpoint_target.squad_uuids)
        and current.external_squad_uuid == checkpoint_target.external_squad_uuid
    ):
        # ``apply_billing_state`` receives this only after the exact disabled
        # state above was proven in the core; ACTIVE source validation is then
        # deliberately disabled by the caller.
        return GracePanelOverlay(
            status='ACTIVE',
            expire_at=session.overlay.expire_at,
            traffic_limit_bytes=checkpoint_target.traffic_limit_bytes,
            squad_uuids=checkpoint_target.squad_uuids,
            external_squad_uuid=checkpoint_target.external_squad_uuid,
        )
    return None


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
        current.remnawave_id == before.remnawave_id
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


def webhook_matches_expired_restore(
    payload: Mapping[str, Any],
    session: GraceAccessSession,
) -> bool:
    """Strictly identify either user.modified phase of an EXPIRED restore.

    The restore checkpoint timestamp plus every Grace-owned field from
    Remnawave's full-user webhook model distinguishes both the status-only
    fail-closed phase and the canonical field phase from later manual updates.
    """
    if not _session_can_match_expired_restore(session):
        return False
    if session.state is GraceSessionState.COMPLETED and session.completion_reason not in {
        GraceCompletionReason.TIMEOUT,
        GraceCompletionReason.DRAINED,
        GraceCompletionReason.REVOKED,
        GraceCompletionReason.CONFLICT,
    }:
        return False

    id_present, payload_id = _webhook_payload_value(payload, 'id')
    try:
        identity_matches = id_present and int(payload_id) == session.remnawave_id
    except (TypeError, ValueError):
        identity_matches = False
    if not identity_matches:
        return False

    updated_present, raw_updated_at = _webhook_payload_value(payload, 'updatedAt')
    updated_at = _parse_datetime(raw_updated_at) if updated_present else None
    if not _restore_echo_timestamp_matches(updated_at, session):
        return False

    status_present, raw_status = _webhook_payload_value(payload, 'status')
    if not status_present:
        return False
    normalized_status = _normalize_status(raw_status)

    expire_present, raw_expire_at = _webhook_payload_value(payload, 'expireAt')
    expire_at = _parse_datetime(raw_expire_at) if expire_present else None
    if not expire_present or expire_at is None:
        return False
    overlay_expiry_matches = _datetimes_equal(expire_at, session.overlay.expire_at)
    before_status = _normalize_status(session.panel_before.status)
    canonical_expiry_matches = overlay_expiry_matches or (
        before_status == 'disabled'
        and session.panel_before.expire_at is not None
        and _datetimes_equal(expire_at, session.panel_before.expire_at)
    )

    limit_present, raw_limit = _webhook_payload_value(payload, 'trafficLimitBytes')
    squads_present, raw_squads = _webhook_payload_value(payload, 'activeInternalSquads')
    external_present, external_squad_uuid = _webhook_payload_value(payload, 'externalSquadUuid')
    if not limit_present or not squads_present or not external_present:
        return False
    try:
        traffic_limit_bytes = int(raw_limit)
    except (TypeError, ValueError):
        return False
    squad_uuids = set(_extract_squad_uuids(raw_squads))

    canonical_phase_matches = (
        canonical_expiry_matches
        and normalized_status in _expected_expired_restore_statuses(session)
        and traffic_limit_bytes == session.panel_before.traffic_limit_bytes
        and squad_uuids == set(session.panel_before.squad_uuids)
        and external_squad_uuid == session.panel_before.external_squad_uuid
    )
    disabled_overlay_phase_matches = (
        overlay_expiry_matches
        and normalized_status == 'disabled'
        and traffic_limit_bytes == session.overlay.traffic_limit_bytes
        and squad_uuids == set(session.overlay.squad_uuids)
        and external_squad_uuid == session.overlay.external_squad_uuid
    )
    return canonical_phase_matches or disabled_overlay_phase_matches


def webhook_matches_traffic_reset_intermediate(
    payload: Mapping[str, Any],
    session: GraceAccessSession,
) -> bool:
    """Strictly identify a delayed quota-fence ``user.modified`` echo.

    A tariff-switch reset first replaces the absolute Grace limit with the
    remaining quota.  Remnawave may deliver that intermediate webhook after
    the final canonical PATCH, so completed reset sessions retain this exact
    fingerprint for a short, timestamp-bounded suppression check.
    """
    if not _traffic_reset_webhook_identity_matches(payload, session):
        return False

    status_present, raw_status = _webhook_payload_value(payload, 'status')
    expire_present, raw_expire_at = _webhook_payload_value(payload, 'expireAt')
    limit_present, raw_limit = _webhook_payload_value(payload, 'trafficLimitBytes')
    squads_present, raw_squads = _webhook_payload_value(payload, 'activeInternalSquads')
    external_present, external_squad_uuid = _webhook_payload_value(payload, 'externalSquadUuid')
    if not all(
        (
            status_present,
            expire_present,
            limit_present,
            squads_present,
            external_present,
        )
    ):
        return False

    expire_at = _parse_datetime(raw_expire_at)
    try:
        traffic_limit_bytes = int(raw_limit)
    except (TypeError, ValueError):
        return False
    if expire_at is None:
        return False

    expected_statuses = {'active', 'limited'}
    if _as_utc(session.completed_at or session.updated_at) >= _as_utc(session.overlay.expire_at):
        expected_statuses.update({'expired', 'disabled'})
    return (
        _normalize_status(raw_status) in expected_statuses
        and _datetimes_equal(expire_at, session.overlay.expire_at)
        and traffic_limit_bytes == max(1, session.traffic_reset_remaining_bytes)
        and set(_extract_squad_uuids(raw_squads)) == set(session.overlay.squad_uuids)
        and external_squad_uuid == session.overlay.external_squad_uuid
    )


def webhook_matches_traffic_reset_signal(
    payload: Mapping[str, Any],
    session: GraceAccessSession,
) -> bool:
    """Match a reset-generated enabled/traffic-reset event with sparse fields."""
    if not _traffic_reset_webhook_identity_matches(payload, session):
        return False

    status_present, raw_status = _webhook_payload_value(payload, 'status')
    if status_present and _normalize_status(raw_status) != 'active':
        return False

    expire_present, raw_expire_at = _webhook_payload_value(payload, 'expireAt')
    if expire_present:
        expire_at = _parse_datetime(raw_expire_at)
        if expire_at is None or not _datetimes_equal(
            expire_at,
            session.overlay.expire_at,
        ):
            return False

    limit_present, raw_limit = _webhook_payload_value(payload, 'trafficLimitBytes')
    if limit_present:
        try:
            traffic_limit_bytes = int(raw_limit)
        except (TypeError, ValueError):
            return False
        if traffic_limit_bytes != max(1, session.traffic_reset_remaining_bytes or 0):
            return False

    squads_present, raw_squads = _webhook_payload_value(payload, 'activeInternalSquads')
    if squads_present and set(_extract_squad_uuids(raw_squads)) != set(
        session.overlay.squad_uuids
    ):
        return False

    external_present, external_squad_uuid = _webhook_payload_value(
        payload,
        'externalSquadUuid',
    )
    return not external_present or (
        external_squad_uuid == session.overlay.external_squad_uuid
    )


def _traffic_reset_webhook_identity_matches(
    payload: Mapping[str, Any],
    session: GraceAccessSession,
) -> bool:
    if (
        session.state is not GraceSessionState.COMPLETED
        or session.traffic_reset_remaining_bytes is None
    ):
        return False
    id_present, payload_id = _webhook_payload_value(payload, 'id')
    try:
        identity_matches = id_present and int(payload_id) == session.remnawave_id
    except (TypeError, ValueError):
        identity_matches = False
    if not identity_matches:
        return False
    updated_present, raw_updated_at = _webhook_payload_value(payload, 'updatedAt')
    updated_at = _parse_datetime(raw_updated_at) if updated_present else None
    return _traffic_reset_echo_timestamp_matches(updated_at, session)


def _session_can_match_expired_restore(session: GraceAccessSession) -> bool:
    if session.reason is not GraceReason.EXPIRED:
        return False
    if session.state not in {GraceSessionState.RESTORING, GraceSessionState.COMPLETED}:
        return False
    before_status = _normalize_status(session.panel_before.status)
    before_expire_at = session.panel_before.expire_at
    return before_status in {'expired', 'disabled'} or bool(
        before_expire_at and _as_utc(before_expire_at) <= _as_utc(session.started_at)
    )


def _expected_expired_restore_statuses(session: GraceAccessSession) -> frozenset[str]:
    before_status = _normalize_status(session.panel_before.status)
    if session.state is GraceSessionState.RESTORING:
        # RESTORING is persisted before either a natural EXPIRED restore or a
        # forced DISABLED drain. The completion reason is not durable yet, so
        # accept both only inside the strict full-field/timestamp fingerprint.
        return frozenset({'disabled', 'expired'})
    if session.completion_reason is GraceCompletionReason.DRAINED:
        # A force drain may run before the watchdog (generic DISABLED restore)
        # or after it has already derived EXPIRED (field-only restore).
        return frozenset({'disabled', 'expired'})
    if before_status == 'disabled':
        # A field-only restore deliberately keeps a watchdog-derived EXPIRED
        # rather than emitting user.disabled for an already expired user.
        return frozenset({'disabled', 'expired'})
    if before_status != 'expired':
        return frozenset({'disabled', 'expired'})
    return frozenset({'expired'})


def _restore_echo_timestamp_matches(
    panel_updated_at: datetime | None,
    session: GraceAccessSession,
) -> bool:
    if panel_updated_at is None:
        return False
    lower_bound = _as_utc(session.updated_at) - _RESTORE_ECHO_TIMESTAMP_TOLERANCE
    upper_reference = session.completed_at or session.updated_at
    upper_bound = _as_utc(upper_reference) + _RESTORE_ECHO_TIMESTAMP_TOLERANCE
    normalized_updated_at = _as_utc(panel_updated_at)
    return lower_bound <= normalized_updated_at <= upper_bound


def _traffic_reset_echo_timestamp_matches(
    panel_updated_at: datetime | None,
    session: GraceAccessSession,
) -> bool:
    if (
        panel_updated_at is None
        or session.traffic_reset_started_at is None
        or session.traffic_reset_finished_at is None
    ):
        return False
    lower_bound = (
        _as_utc(session.traffic_reset_started_at)
        - _RESTORE_ECHO_TIMESTAMP_TOLERANCE
    )
    upper_bound = (
        _as_utc(session.traffic_reset_finished_at)
        + _RESTORE_ECHO_TIMESTAMP_TOLERANCE
    )
    normalized_updated_at = _as_utc(panel_updated_at)
    return lower_bound <= normalized_updated_at <= upper_bound


def _webhook_payload_value(payload: Mapping[str, Any], key: str) -> tuple[bool, Any]:
    if key in payload:
        return True, payload.get(key)
    nested_user = payload.get('user')
    if isinstance(nested_user, Mapping) and key in nested_user:
        return True, nested_user.get(key)
    return False, None


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


def _reset_generations_equal(left: datetime | None, right: datetime | None) -> bool:
    """Compare reset generations exactly so rapid consecutive resets are visible."""
    if left is None or right is None:
        return left is right
    return _as_utc(left) == _as_utc(right)


def _bounded_traffic_reset_finished_at(
    session: GraceAccessSession,
    *,
    observed_at: datetime | None,
    now: datetime,
) -> datetime:
    """Bound Remnawave's reset generation to the local durable operation window."""
    started_at = _as_utc(session.traffic_reset_started_at or session.updated_at)
    local_now = _as_utc(now)
    candidate = _as_utc(observed_at) if observed_at is not None else local_now
    upper_bound = max(local_now, started_at)
    return min(max(candidate, started_at), upper_bound)


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
