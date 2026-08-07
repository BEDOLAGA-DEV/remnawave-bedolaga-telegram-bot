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
_MUTATION_ECHO_MAX_WINDOW = timedelta(minutes=5)
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
    EXTERNAL_RESET_REVOKED = 'external_reset_revoked'
    FAIL_CLOSED_REVOKED = 'fail_closed_revoked'


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
    # Remnawave traffic reset strategy. ``None`` is reserved for legacy
    # snapshots that predate the strategy field and must be hydrated from the
    # live panel before any mutation.
    traffic_limit_strategy: str | None = None
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
    # ``None`` is an explicit legacy marker for v2/v3 snapshots only.
    traffic_limit_strategy: str | None = None


@dataclass(frozen=True, slots=True)
class GracePanelOverlay:
    """Exact temporary state expected in Remnawave while grace is active."""

    status: str
    expire_at: datetime
    traffic_limit_bytes: int
    squad_uuids: tuple[str, ...]
    external_squad_uuid: str | None = None
    traffic_limit_strategy: str | None = None
    expected_last_traffic_reset_at: datetime | None = None


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
    # Generation immediately before the persisted reset intent.  The rebased
    # panel snapshot stores the new generation, so delayed quota-fence echoes
    # need this separate exact fingerprint.
    traffic_reset_previous_generation: datetime | None = None
    # Exact usage from the pre-reset panel read. It cannot be reconstructed
    # from remaining bytes once usage has exceeded the temporary quota.
    traffic_reset_previous_used_bytes: int | None = None
    # Generation confirmed by the post-reset GET. This is distinct from the
    # pre-reset generation used by user.enabled and quota-fence echoes.
    traffic_reset_result_generation: datetime | None = None
    # Narrow causal windows for delayed Remnawave user.modified echoes. They are
    # persisted separately from the potentially multi-day Grace lifetime.
    activation_started_at: datetime | None = None
    activation_finished_at: datetime | None = None
    restore_started_at: datetime | None = None
    restore_finished_at: datetime | None = None
    # Distinguish natural timeout restoration (which only waits for EXPIRED)
    # from an explicit drain/revoke path that deliberately writes DISABLED.
    # Webhook suppression must never infer this intent from a broad status set.
    restore_force_disable: bool = False
    # Terminal intent selected before the first restore PATCH. It survives a
    # crash so a later ordinary reconcile cannot turn an early drain/revoke
    # into a natural timeout path or report the wrong completion reason.
    restore_completion_reason: GraceCompletionReason | None = None
    # A v2/v3 row may be observed only after a newer worker has already written
    # NO_RESET. In that case the original strategy cannot be reconstructed from
    # the panel. The fresh canonical strategy is persisted only as a safe
    # convergence target; this marker forbids continuing the Grace grant or
    # restoring the rest of the stale snapshot.
    legacy_strategy_fallback: bool = False
    # Keep the source version until legacy metadata has been resolved and
    # durably checkpointed.  Otherwise an unrelated error save would silently
    # turn a v2/v3 row into a v4 row that still contains unknown strategy data.
    snapshot_version: int = 4


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


@dataclass(frozen=True, slots=True)
class _GraceMetadataHydration:
    session: GraceAccessSession
    checkpointed: bool = False


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

    async def checkpoint(self, session: GraceAccessSession) -> GraceAccessSession:
        """Persist metadata before the next external panel mutation."""
        ...

    async def list_open(self, *, limit: int) -> Sequence[GraceAccessSession]: ...


class GracePanelGateway(Protocol):
    """Remnawave adapter implemented in the next integration step."""

    async def read_snapshot(self, remnawave_id: int) -> GracePanelSnapshot | None: ...

    async def apply_overlay(
        self,
        remnawave_id: int,
        overlay: GracePanelOverlay,
        *,
        expected_source: GracePanelSnapshot,
    ) -> None: ...

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

    async def fail_closed_external_reset(
        self,
        remnawave_id: int,
        *,
        expected_overlay: GracePanelOverlay,
        expected_last_traffic_reset_at: datetime | None,
        observed_last_traffic_reset_at: datetime | None,
    ) -> GraceRestoreOutcome: ...

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
            if lineage_session is None:
                # A panel upgrade may make lastTrafficResetAt available after
                # an older session was stored with the explicit ``unknown``
                # generation. Reuse that durable lineage instead of minting a
                # second quota merely because the key became more precise.
                recent_sessions = await self._store.list_recent_completed(
                    billing.subscription_id,
                    limit=32,
                )
                for candidate in recent_sessions:
                    candidate_tail = candidate.limited_lineage_tail or candidate.billing_before
                    if (
                        candidate.reason is GraceReason.LIMITED
                        and candidate_tail.remnawave_id == billing.remnawave_id
                        and candidate.panel_before.last_traffic_reset_at is None
                        and panel_snapshot.last_traffic_reset_at is not None
                        and candidate_tail.end_at == billing.end_at
                    ):
                        lineage_session = candidate
                        break
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
                await self._keep_restoring_after_conflict(
                    latest_session,
                    last_error=_error_text(error),
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
        if normalized_event == 'user.disabled':
            # An administrative disable must always reach canonical billing.
            return False

        session = await self._store.get_open(subscription_id)
        sessions: Sequence[GraceAccessSession]
        if session is not None:
            sessions = (session,)
        else:
            if normalized_event not in {
                'user.modified',
                'user.enabled',
                'user.expired',
                'user.limited',
                'user.traffic_reset',
            }:
                return False
            sessions = await self._store.list_recent_completed(subscription_id, limit=8)

        if normalized_event == 'user.enabled' and session is not None and session.allow_recovery_enabled_webhook:
            fresh_billing = await self._billing.get_subscription(subscription_id)
            if fresh_billing is not None and webhook_matches_billing_recovery(payload, fresh_billing):
                return False

        for candidate in sessions:
            if normalized_event == 'user.modified' and (
                webhook_matches_activation_modified(payload, candidate)
                or webhook_matches_expired_restore(payload, candidate, event_name=normalized_event)
                or webhook_matches_limited_restore(payload, candidate, event_name=normalized_event)
                or webhook_matches_traffic_reset_intermediate(payload, candidate)
            ):
                return True
            if normalized_event == 'user.enabled' and (
                webhook_matches_activation_enabled(payload, candidate)
                or webhook_matches_limited_restore(payload, candidate, event_name=normalized_event)
                or webhook_matches_traffic_reset_enabled(payload, candidate)
            ):
                return True
            if normalized_event == 'user.traffic_reset' and webhook_matches_traffic_reset_completed(
                payload,
                candidate,
            ):
                return True
            if normalized_event in {'user.expired', 'user.limited'} and (
                webhook_matches_overlay_event(payload, candidate, normalized_event)
                or webhook_matches_expired_restore(payload, candidate, event_name=normalized_event)
                or webhook_matches_limited_restore(payload, candidate, event_name=normalized_event)
            ):
                return True
        return False

    async def _hydrate_legacy_metadata(
        self,
        session: GraceAccessSession,
        billing: GraceBillingState | None,
        *,
        current_panel: GracePanelSnapshot | None = None,
    ) -> _GraceMetadataHydration:
        """Resolve v2/v3 strategy metadata before any new panel mutation.

        The reset generation was already captured in ``panel_before`` by the
        legacy implementation, so it is copied from that durable value rather
        than adopted from a later live read.  The original strategy is accepted
        only from an exact known legacy phase.  If a newer process already
        changed it to NO_RESET before persisting the original, canonical billing
        is used as an explicit fail-closed restore target and Grace is not
        continued.
        """
        if not session_needs_metadata_hydration(session):
            return _GraceMetadataHydration(session)

        live = current_panel or await self._panel.read_snapshot(session.remnawave_id)
        if live is None:
            return _GraceMetadataHydration(session)
        if live.remnawave_id != session.remnawave_id:
            raise GracePanelTransitionConflict('Legacy Grace metadata belongs to another Remnawave user')
        canonical_completion_reason: GraceCompletionReason | None = None
        if billing is not None:
            if billing_has_recovered(session, billing):
                canonical_completion_reason = GraceCompletionReason.PAID
            elif billing_is_revoked(billing):
                canonical_completion_reason = GraceCompletionReason.REVOKED
        if canonical_completion_reason is not None and billing is not None:
            from app.services.grace_access_runtime import _build_billing_target, _panel_matches_target

            canonical_target = _build_billing_target(billing, now=_as_utc(self._clock()))
            if _panel_matches_target(live, canonical_target):
                # Recovery/revocation PATCH already reached Remnawave before the
                # old Grace row was closed. Let the ordinary canonical branch
                # finish it; hydration must not strand the row merely because
                # the panel is no longer a legacy phase.
                return _GraceMetadataHydration(session)
        if not _reset_generations_equal(
            live.last_traffic_reset_at,
            session.panel_before.last_traffic_reset_at,
        ):
            # A paid/recovered canonical state must never authenticate itself as
            # a Grace phase merely because it is the latest panel observation.
            # The caller will converge it through apply_billing_state using the
            # persisted overlay CAS proof. Any unrelated panel state remains a
            # conflict and is never disabled here.
            if not panel_is_legacy_hydration_source(live, session):
                raise GracePanelTransitionConflict('Reset generation changed outside every exact legacy Grace phase')
            observed_overlay = _overlay_from_observed_panel(live)
            if canonical_completion_reason is not None and billing is not None:
                await self._panel.apply_billing_state(
                    billing,
                    expected_overlay=observed_overlay,
                    expected_last_traffic_reset_at=live.last_traffic_reset_at,
                )
                completed = await self._complete(session, canonical_completion_reason)
                return _GraceMetadataHydration(completed)
            outcome = await self._panel.fail_closed_external_reset(
                session.remnawave_id,
                expected_overlay=observed_overlay,
                expected_last_traffic_reset_at=session.panel_before.last_traffic_reset_at,
                observed_last_traffic_reset_at=live.last_traffic_reset_at,
            )
            message = 'External Remnawave traffic reset happened before legacy Grace metadata hydration'
            if outcome is GraceRestoreOutcome.CONFLICT:
                restoring = await self._keep_restoring_after_conflict(
                    session,
                    last_error=f'{message}; fail-closed revocation is not confirmed',
                )
                return _GraceMetadataHydration(restoring)
            completed = await self._complete(
                session,
                GraceCompletionReason.CONFLICT,
                last_error=f'{message}; access was revoked fail-closed',
            )
            return _GraceMetadataHydration(completed)
        if not panel_is_legacy_hydration_source(live, session):
            raise GracePanelTransitionConflict(
                'Remnawave state is not an exact legacy Grace phase; strategy was not adopted'
            )

        live_strategy = _concrete_strategy(live.traffic_limit_strategy)
        if live_strategy is None:
            raise GracePanelTransitionPending('Remnawave did not return a concrete strategy for legacy Grace hydration')

        original_strategy = _concrete_strategy(session.panel_before.traffic_limit_strategy)
        canonical_fallback_required = session.legacy_strategy_fallback
        if original_strategy is None:
            if live_strategy != 'NO_RESET':
                original_strategy = live_strategy
            elif (
                billing is not None
                and billing.remnawave_id == session.remnawave_id
                and _concrete_strategy(billing.traffic_limit_strategy) is not None
            ):
                # NO_RESET is an irreversible ambiguity here: it may be the
                # phase written by a crashed newer worker.  Never record it as
                # the original.  A fresh billing strategy is authoritative only
                # as a canonical convergence target, so this session must close.
                original_strategy = _concrete_strategy(billing.traffic_limit_strategy)
                canonical_fallback_required = True
            else:
                # Without a canonical strategy for this exact panel user there
                # is no safe restore target. Revoke the precisely observed
                # legacy phase and leave no ACTIVE/LIMITED access behind.
                await self._panel.revoke_missing_billing(
                    session.remnawave_id,
                    expected_overlay=_overlay_from_observed_panel(live),
                )
                completed = await self._complete(
                    session,
                    GraceCompletionReason.CONFLICT,
                    last_error='Legacy Grace strategy is unknown and canonical billing cannot identify it',
                )
                return _GraceMetadataHydration(completed)
        elif live_strategy not in {original_strategy, 'NO_RESET'}:
            raise GracePanelTransitionConflict('Remnawave strategy changed outside the persisted Grace phases')

        hydrated_billing = session.billing_before
        if (
            hydrated_billing.traffic_limit_strategy is None
            and billing is not None
            and billing_still_matches_session(session, billing)
            and _concrete_strategy(billing.traffic_limit_strategy) is not None
        ):
            hydrated_billing = replace(
                hydrated_billing,
                traffic_limit_strategy=_concrete_strategy(billing.traffic_limit_strategy),
            )

        hydrated = replace(
            session,
            billing_before=hydrated_billing,
            panel_before=replace(
                session.panel_before,
                traffic_limit_strategy=original_strategy,
            ),
            overlay=replace(
                session.overlay,
                traffic_limit_strategy='NO_RESET',
                expected_last_traffic_reset_at=session.panel_before.last_traffic_reset_at,
            ),
            snapshot_version=4,
            legacy_strategy_fallback=canonical_fallback_required,
            updated_at=_as_utc(self._clock()),
            last_error=(
                'Legacy original strategy was unavailable; canonical strategy restore is required'
                if canonical_fallback_required
                else None
            ),
        )
        hydrated = await self._store.checkpoint(hydrated)
        if hydrated.state is GraceSessionState.COMPLETED:
            return _GraceMetadataHydration(hydrated)
        # checkpoint() commits and reacquires the subscription lock. Every
        # billing/panel read above is now stale by definition. The caller must
        # restart from the returned CAS winner before any external mutation.
        return _GraceMetadataHydration(hydrated, checkpointed=True)

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

        hydration = await self._hydrate_legacy_metadata(
            session,
            latest_billing,
            current_panel=current_panel,
        )
        session = hydration.session
        if session.state is GraceSessionState.COMPLETED:
            return session
        if hydration.checkpointed:
            if session.state is not GraceSessionState.PENDING:
                # checkpoint() commits and reacquires the lock. A concurrent
                # worker may already have won RESTORING/ACTIVE/COMPLETED; never
                # run an activation PATCH from that winner.
                return session
            if session_needs_metadata_hydration(session):
                raise GracePanelTransitionPending('Legacy Grace metadata checkpoint lost its optimistic CAS race')
            # checkpoint() released/reacquired the database lock. Restart every
            # eligibility and panel predicate from fresh state before a PATCH.
            return await self._activate_pending(session)
        if session.legacy_strategy_fallback:
            _, completed = await self._restore_and_complete(
                session,
                GraceCompletionReason.CONFLICT,
            )
            return completed

        overlay_is_already_applied = panel_matches_overlay(
            current_panel,
            session.overlay,
            now=now,
        )
        legacy_overlay_phase = panel_matches_legacy_overlay(current_panel, session)
        if (
            not overlay_is_already_applied
            and not legacy_overlay_phase
            and not panel_status_matches_reason(current_panel.status, session.reason)
        ):
            if _normalize_status(current_panel.status) == 'active':
                return await self._keep_restoring_after_conflict(
                    session,
                    last_error=('Unexpected ACTIVE remains different from canonical billing; restore is pending'),
                )
            return await self._complete(
                session,
                GraceCompletionReason.CONFLICT,
                last_error=(
                    'Grace source status no longer matches the incident; manual DISABLED state was not enabled'
                ),
            )
        if (
            not overlay_is_already_applied
            and not legacy_overlay_phase
            and not panel_is_safe_pending_source(
                current_panel,
                session.panel_before,
                session.overlay,
            )
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
            activation_attempt_expired = bool(
                session.activation_started_at is not None
                and now >= _as_utc(session.activation_started_at) + _MUTATION_ECHO_MAX_WINDOW
            )
            if session.activation_started_at is None or activation_attempt_expired:
                checkpointed = await self._store.checkpoint(
                    replace(
                        session,
                        activation_started_at=_as_utc(self._clock()),
                        activation_finished_at=None,
                        updated_at=_as_utc(self._clock()),
                    )
                )
                if checkpointed.state is not GraceSessionState.PENDING:
                    return checkpointed
                if checkpointed.activation_started_at is None:
                    raise GracePanelTransitionPending('Grace activation checkpoint lost its optimistic CAS race')
                # The checkpoint released the lock. Repeat every source and
                # billing predicate before the first panel PATCH.
                return await self._activate_pending(checkpointed)
            try:
                await self._panel.apply_overlay(
                    session.remnawave_id,
                    session.overlay,
                    expected_source=session.panel_before,
                )
            except Exception as error:
                failed_session = replace(
                    session,
                    updated_at=_as_utc(self._clock()),
                    last_error=_error_text(error),
                )
                await self._store.save(failed_session)
                raise
            session = replace(
                session,
                activation_finished_at=_as_utc(self._clock()),
            )
        elif session.activation_started_at is not None and session.activation_finished_at is None:
            # Crash recovery can observe the exact final overlay after the API
            # mutation but before the ACTIVE save. Bound the proof window to the
            # durable attempt and finish it without issuing another PATCH.
            session = replace(
                session,
                activation_finished_at=_as_utc(self._clock()),
            )

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
        hydration = await self._hydrate_legacy_metadata(session, billing)
        session = hydration.session
        if session.state is GraceSessionState.COMPLETED:
            return (session.completion_reason or GraceCompletionReason.CONFLICT).value
        if hydration.checkpointed:
            # The checkpoint deliberately committed/relocked. A later pass must
            # re-read billing and panel before doing any external mutation.
            return 'repaired'
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
                    return (session.completion_reason or GraceCompletionReason.CONFLICT).value
                await self._panel.apply_billing_state(
                    billing,
                    expected_overlay=session.overlay,
                    expected_restored_snapshot=(
                        session.panel_before if session.state is GraceSessionState.RESTORING else None
                    ),
                )
                await self._complete(session, GraceCompletionReason.CONFLICT)
                return GraceCompletionReason.CONFLICT.value
            action, _ = await self._restore_and_complete(session, GraceCompletionReason.CONFLICT)
            return action

        if session.legacy_strategy_fallback:
            # The original v2/v3 strategy was already lost behind NO_RESET.
            # A proven tariff switch above may safely establish a fresh restore
            # point. With otherwise unchanged billing, canonical convergence is
            # the only safe action and the stale Grace grant must close.
            if billing.remnawave_id != session.remnawave_id:
                await self._panel.revoke_missing_billing(
                    session.remnawave_id,
                    expected_overlay=session.overlay,
                )
            else:
                await self._panel.apply_billing_state(
                    billing,
                    expected_overlay=session.overlay,
                    expected_last_traffic_reset_at=session.panel_before.last_traffic_reset_at,
                )
            await self._complete(
                session,
                GraceCompletionReason.CONFLICT,
                last_error='Legacy Grace strategy was recovered only from canonical billing',
            )
            return GraceCompletionReason.CONFLICT.value

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
            if panel_matches_legacy_overlay(current_panel, session):
                await self._panel.apply_overlay(
                    session.remnawave_id,
                    session.overlay,
                    expected_source=session.panel_before,
                )
                return 'repaired'
            expected_reset_generation = session.panel_before.last_traffic_reset_at
            observed_reset_generation = current_panel.last_traffic_reset_at
            if session.traffic_reset_target is None and not _reset_generations_equal(
                observed_reset_generation,
                expected_reset_generation,
            ):
                outcome = await self._panel.fail_closed_external_reset(
                    session.remnawave_id,
                    expected_overlay=session.overlay,
                    expected_last_traffic_reset_at=expected_reset_generation,
                    observed_last_traffic_reset_at=observed_reset_generation,
                )
                error_message = 'External Remnawave traffic reset generation changed during Grace access'
                if outcome is GraceRestoreOutcome.CONFLICT:
                    await self._keep_restoring_after_conflict(
                        session,
                        last_error=f'{error_message}; fail-closed revocation is not confirmed',
                    )
                    return GraceCompletionReason.CONFLICT.value
                await self._complete(
                    session,
                    GraceCompletionReason.CONFLICT,
                    last_error=f'{error_message}; access was revoked fail-closed',
                )
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
                verified_panel = await self._panel.read_snapshot(session.remnawave_id)
                from app.services.grace_access_runtime import _build_billing_target, _panel_matches_target

                canonical_target = _build_billing_target(billing, now=now)
                if verified_panel is not None and _panel_matches_target(verified_panel, canonical_target):
                    await self._complete(
                        session,
                        GraceCompletionReason.CONFLICT,
                        last_error='Unexpected active Remnawave state was replaced by canonical billing',
                    )
                else:
                    await self._store.save(
                        replace(
                            session,
                            state=GraceSessionState.RESTORING,
                            updated_at=_as_utc(self._clock()),
                            last_error='Unexpected ACTIVE remains different from canonical billing; restore is pending',
                        )
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
                traffic_reset_previous_generation=session.panel_before.last_traffic_reset_at,
                traffic_reset_previous_used_bytes=current_panel.used_traffic_bytes,
                traffic_reset_result_generation=None,
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
                traffic_reset_started_at=(session.traffic_reset_started_at or session.updated_at),
                traffic_reset_finished_at=reset_finished_at,
                traffic_reset_result_generation=reset_result.panel.last_traffic_reset_at,
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
            for value in dict.fromkeys((*session.incident_aliases, current_incident_key, lineage_key))
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
            traffic_limit_strategy=target.traffic_limit_strategy,
            last_traffic_reset_at=reset_result.panel.last_traffic_reset_at,
        )
        continued_overlay = replace(
            reset_result.overlay,
            expected_last_traffic_reset_at=reset_result.panel.last_traffic_reset_at,
        )
        continued = replace(
            session,
            billing_before=target,
            panel_before=rebased_panel,
            overlay=continued_overlay,
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
            raise GracePanelTransitionConflict('Remnawave user disappeared while the tariff reset target was changing')
        expected_source = traffic_reset_checkpoint_source_overlay(
            session,
            current_panel,
            checkpoint_target=checkpoint_target,
            remaining_bytes=remaining_bytes,
            now=now,
        )
        if expected_source is None:
            raise GracePanelTransitionConflict('Remnawave changed outside the persisted tariff reset checkpoint')

        reset_generation_changed = not _reset_generations_equal(
            current_panel.last_traffic_reset_at,
            session.panel_before.last_traffic_reset_at,
        )
        if reset_generation_changed and (
            session.traffic_reset_finished_at is None
            or not _reset_generations_equal(
                session.traffic_reset_result_generation,
                current_panel.last_traffic_reset_at,
            )
        ):
            session = await self._store.save(
                replace(
                    session,
                    traffic_reset_finished_at=_bounded_traffic_reset_finished_at(
                        session,
                        observed_at=current_panel.last_traffic_reset_at,
                        now=now,
                    ),
                    traffic_reset_result_generation=current_panel.last_traffic_reset_at,
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
            subscription_is_unexpired = bool(billing.end_at is not None and _as_utc(billing.end_at) > now)
            new_tariff_has_access = (
                billing.traffic_limit_bytes == 0 or current_panel.used_traffic_bytes < billing.traffic_limit_bytes
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
                        return (recovering_session.completion_reason or GraceCompletionReason.CONFLICT).value

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
                        GraceCompletionReason.REVOKED if fresh_billing is None else GraceCompletionReason.CONFLICT
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
            traffic_limit_strategy=billing.traffic_limit_strategy,
        )
        rebased = replace(
            session,
            billing_before=billing,
            panel_before=rebased_panel,
            incident_aliases=aliases,
            limited_lineage_tail=billing,
            allow_recovery_enabled_webhook=False,
            legacy_strategy_fallback=False,
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
        if session.reason is not GraceReason.LIMITED or not tariff_change_matches_incident_family(
            session, billing, self._policy
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
        aliases = tuple(dict.fromkeys((*session.incident_aliases, current_incident_key, lineage_key)))
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
        effective_completion_reason = session.restore_completion_reason or completion_reason
        if (
            effective_completion_reason is GraceCompletionReason.TIMEOUT
            and completion_reason is not GraceCompletionReason.TIMEOUT
        ):
            # Emergency restore-all/drain is a monotonic escalation. It may
            # force a natural TIMEOUT restore to DISABLED, while a later normal
            # reconcile must never downgrade an already forced terminal intent.
            effective_completion_reason = completion_reason
        # Refresh the durable checkpoint before every external restore attempt.
        # Besides crash recovery, this timestamps the narrow window in which a
        # full user.modified payload can be proven to be our own restore echo.
        restoring_session = replace(
            session,
            state=GraceSessionState.RESTORING,
            restore_started_at=now,
            restore_finished_at=None,
            restore_force_disable=(
                session.restore_force_disable or effective_completion_reason is not GraceCompletionReason.TIMEOUT
            ),
            restore_completion_reason=effective_completion_reason,
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
            force_disable=restoring_session.restore_force_disable,
        )

        post_restore_session = restoring_session
        if outcome in {
            GraceRestoreOutcome.RESTORED,
            GraceRestoreOutcome.ALREADY_RESTORED,
        }:
            post_restore_session = replace(
                restoring_session,
                restore_finished_at=_as_utc(self._clock()),
            )

        # Payment may land after the pre-restore check.  Paid billing always wins
        # over an old snapshot, even if the restore PATCH has already succeeded.
        latest_billing = await self._billing.get_subscription(session.subscription_id)
        fresh_completion = await self._complete_for_fresh_billing_change(
            post_restore_session,
            latest_billing,
            expected_restored_snapshot=restoring_session.panel_before,
        )
        if fresh_completion is not None:
            return fresh_completion

        if outcome is GraceRestoreOutcome.CONFLICT:
            restoring = await self._keep_restoring_after_conflict(
                restoring_session,
                last_error='Remnawave state changed outside grace; automatic restore was not applied',
            )
            return GraceCompletionReason.CONFLICT.value, restoring

        if outcome is GraceRestoreOutcome.EXTERNAL_RESET_REVOKED:
            completed = await self._complete(
                restoring_session,
                GraceCompletionReason.CONFLICT,
                last_error='External Remnawave traffic reset was detected; access was revoked fail-closed',
            )
            return GraceCompletionReason.CONFLICT.value, completed

        if outcome is GraceRestoreOutcome.FAIL_CLOSED_REVOKED:
            completed = await self._complete(
                restoring_session,
                GraceCompletionReason.CONFLICT,
                last_error='Unsafe Remnawave restore phase was revoked fail-closed',
            )
            return GraceCompletionReason.CONFLICT.value, completed

        completed = await self._complete(post_restore_session, effective_completion_reason)
        return effective_completion_reason.value, completed

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
            session.traffic_reset_target is None and session.traffic_reset_remaining_bytes is not None
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
            traffic_reset_remaining_bytes=(session.traffic_reset_remaining_bytes if retain_reset_proof else None),
            traffic_reset_started_at=(
                (session.traffic_reset_started_at or session.updated_at) if retain_reset_proof else None
            ),
            traffic_reset_finished_at=(session.traffic_reset_finished_at if retain_reset_proof else None),
            traffic_reset_previous_generation=(
                session.traffic_reset_previous_generation if retain_reset_proof else None
            ),
            traffic_reset_previous_used_bytes=(
                session.traffic_reset_previous_used_bytes if retain_reset_proof else None
            ),
            traffic_reset_result_generation=(session.traffic_reset_result_generation if retain_reset_proof else None),
        )
        return await self._store.save(completed_session)

    async def _keep_restoring_after_conflict(
        self,
        session: GraceAccessSession,
        *,
        last_error: str,
    ) -> GraceAccessSession:
        """Keep the durable guard alive until unsafe panel access is resolved."""
        now = _as_utc(self._clock())
        restoring_session = replace(
            session,
            state=GraceSessionState.RESTORING,
            completion_reason=None,
            completed_at=None,
            updated_at=now,
            last_error=last_error,
            allow_recovery_enabled_webhook=False,
        )
        return await self._store.save(restoring_session)

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
    strictly_larger = (current_limit == 0 and previous_limit != 0) or (
        current_limit != 0 and previous_limit != 0 and current_limit > previous_limit
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
        and current.traffic_limit_strategy == target.traffic_limit_strategy
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
    tariff_matches = not before.tariff_id_known or not current.tariff_id_known or current.tariff_id == before.tariff_id
    strategy_matches = (
        before.traffic_limit_strategy is None or current.traffic_limit_strategy == before.traffic_limit_strategy
    )
    return (
        current.traffic_limit_bytes == before.traffic_limit_bytes
        and current.device_limit == before.device_limit
        and set(current.squad_uuids) == set(before.squad_uuids)
        and current.external_squad_uuid == before.external_squad_uuid
        and strategy_matches
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
        return normalized == 'expired'
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
        traffic_limit_strategy='NO_RESET',
        expected_last_traffic_reset_at=snapshot.last_traffic_reset_at,
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
            traffic_limit_strategy=current.traffic_limit_strategy,
            expected_last_traffic_reset_at=current.last_traffic_reset_at,
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
            traffic_limit_strategy=current.traffic_limit_strategy,
            expected_last_traffic_reset_at=current.last_traffic_reset_at,
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
        and overlay.traffic_limit_strategy is not None
        and snapshot.traffic_limit_strategy == overlay.traffic_limit_strategy
        and _reset_generations_equal(
            snapshot.last_traffic_reset_at,
            overlay.expected_last_traffic_reset_at,
        )
    )


def panel_is_safe_pending_source(
    current: GracePanelSnapshot,
    before: GracePanelSnapshot,
    overlay: GracePanelOverlay,
) -> bool:
    """Recognize only states that this PENDING activation could have produced.

    Used traffic is intentionally ignored because it is monotonic accounting
    data.  Exact original, external-detached and NO_RESET-confirmed states are
    the only accepted pre-final phases.
    """
    if before.traffic_limit_strategy is None or overlay.traffic_limit_strategy != 'NO_RESET':
        return False
    unchanged_core = (
        current.remnawave_id == before.remnawave_id
        and _normalize_status(current.status) == _normalize_status(before.status)
        and _datetimes_equal(current.expire_at, before.expire_at)
        and current.traffic_limit_bytes == before.traffic_limit_bytes
        and set(current.squad_uuids) == set(before.squad_uuids)
        and _reset_generations_equal(
            current.last_traffic_reset_at,
            overlay.expected_last_traffic_reset_at,
        )
    )
    if not unchanged_core:
        return False
    original_phase = (
        current.external_squad_uuid == before.external_squad_uuid
        and current.traffic_limit_strategy == before.traffic_limit_strategy
    )
    detached_phase = (
        current.external_squad_uuid == overlay.external_squad_uuid
        and current.traffic_limit_strategy == before.traffic_limit_strategy
    )
    no_reset_phase = (
        current.external_squad_uuid == overlay.external_squad_uuid
        and current.traffic_limit_strategy == overlay.traffic_limit_strategy
    )
    return original_phase or detached_phase or no_reset_phase


def session_needs_metadata_hydration(session: GraceAccessSession) -> bool:
    return (
        session.snapshot_version < 4
        or session.panel_before.traffic_limit_strategy is None
        or session.overlay.traffic_limit_strategy != 'NO_RESET'
        or not _reset_generations_equal(
            session.overlay.expected_last_traffic_reset_at,
            session.panel_before.last_traffic_reset_at,
        )
    )


def _overlay_from_observed_panel(snapshot: GracePanelSnapshot) -> GracePanelOverlay:
    """Build an exact CAS fingerprint for fail-closed legacy recovery."""
    if snapshot.expire_at is None:
        raise GracePanelTransitionConflict('Remnawave omitted expireAt from a legacy Grace phase')
    strategy = _concrete_strategy(snapshot.traffic_limit_strategy)
    if strategy is None:
        raise GracePanelTransitionConflict('Remnawave omitted traffic strategy from a legacy Grace phase')
    return GracePanelOverlay(
        status=str(snapshot.status),
        expire_at=snapshot.expire_at,
        traffic_limit_bytes=snapshot.traffic_limit_bytes,
        squad_uuids=snapshot.squad_uuids,
        external_squad_uuid=snapshot.external_squad_uuid,
        traffic_limit_strategy=strategy,
        expected_last_traffic_reset_at=snapshot.last_traffic_reset_at,
    )


def panel_matches_legacy_overlay(
    snapshot: GracePanelSnapshot,
    session: GraceAccessSession,
) -> bool:
    """Match an old overlay that predates Grace-owned NO_RESET protection."""
    original_strategy = session.panel_before.traffic_limit_strategy
    if original_strategy is None or snapshot.traffic_limit_strategy != original_strategy:
        return False
    allowed_statuses = {'active', 'limited', 'expired'}
    if session.state is GraceSessionState.RESTORING:
        allowed_statuses.add('disabled')
    return (
        snapshot.remnawave_id == session.remnawave_id
        and _normalize_status(snapshot.status) in allowed_statuses
        and _datetimes_equal(snapshot.expire_at, session.overlay.expire_at)
        and snapshot.traffic_limit_bytes == session.overlay.traffic_limit_bytes
        and set(snapshot.squad_uuids) == set(session.overlay.squad_uuids)
        and snapshot.external_squad_uuid == session.overlay.external_squad_uuid
        and _reset_generations_equal(
            snapshot.last_traffic_reset_at,
            session.panel_before.last_traffic_reset_at,
        )
    )


def panel_is_legacy_hydration_source(
    current: GracePanelSnapshot,
    session: GraceAccessSession,
) -> bool:
    """Recognize exact states emitted by the pre-v4 or phased implementation."""
    before = session.panel_before
    overlay = session.overlay
    if current.remnawave_id != session.remnawave_id:
        return False

    before_statuses = {_normalize_status(before.status)}
    if session.state is GraceSessionState.RESTORING:
        if _normalize_status(before.status) in {'expired', 'disabled'}:
            before_statuses.update({'expired', 'disabled'})
        elif _normalize_status(before.status) == 'limited':
            before_statuses.update({'limited', 'expired'})
    before_phase = (
        _normalize_status(current.status) in before_statuses
        and _datetimes_equal(current.expire_at, before.expire_at)
        and current.traffic_limit_bytes == before.traffic_limit_bytes
        and set(current.squad_uuids) == set(before.squad_uuids)
        and current.external_squad_uuid
        in {
            before.external_squad_uuid,
            overlay.external_squad_uuid,
        }
    )

    overlay_statuses = {'active', 'limited', 'expired'}
    if session.state is GraceSessionState.RESTORING:
        overlay_statuses.add('disabled')
    overlay_phase = (
        _normalize_status(current.status) in overlay_statuses
        and _datetimes_equal(current.expire_at, overlay.expire_at)
        and current.traffic_limit_bytes == overlay.traffic_limit_bytes
        and set(current.squad_uuids) == set(overlay.squad_uuids)
        and current.external_squad_uuid == overlay.external_squad_uuid
    )
    return before_phase or overlay_phase


def _concrete_strategy(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().upper()
    if normalized in {'NO_RESET', 'DAY', 'WEEK', 'MONTH', 'MONTH_ROLLING'}:
        return normalized
    return None


@dataclass(frozen=True, slots=True)
class _WebhookPanelState:
    remnawave_id: int
    status: str
    expire_at: datetime
    traffic_limit_bytes: int
    traffic_limit_strategy: str
    squad_uuids: tuple[str, ...]
    external_squad_uuid: str | None
    last_traffic_reset_at: datetime | None
    updated_at: datetime
    used_traffic_bytes: int
    device_limit: int | None


def _parse_webhook_panel_state(payload: Mapping[str, Any]) -> _WebhookPanelState | None:
    """Parse the mandatory Remnawave 3.2.1 user-event snapshot."""
    required = (
        'id',
        'status',
        'expireAt',
        'trafficLimitBytes',
        'trafficLimitStrategy',
        'activeInternalSquads',
        'externalSquadUuid',
        'lastTrafficResetAt',
        'updatedAt',
        'hwidDeviceLimit',
        'userTraffic',
    )
    values: dict[str, Any] = {}
    for key in required:
        present, value = _webhook_payload_value(payload, key)
        if not present:
            return None
        values[key] = value

    expire_at = _parse_datetime(values['expireAt'])
    updated_at = _parse_datetime(values['updatedAt'])
    strategy = _concrete_strategy(str(values['trafficLimitStrategy']))
    squads = values['activeInternalSquads']
    user_traffic = values['userTraffic']
    if expire_at is None or updated_at is None or strategy is None:
        return None
    if not isinstance(squads, list) or not isinstance(user_traffic, Mapping):
        return None
    reset_raw = values['lastTrafficResetAt']
    reset_at = _parse_datetime(reset_raw) if reset_raw is not None else None
    if reset_raw is not None and reset_at is None:
        return None
    try:
        remnawave_id = int(values['id'])
        traffic_limit_bytes = int(values['trafficLimitBytes'])
        used_traffic_bytes = int(user_traffic['usedTrafficBytes'])
        raw_device_limit = values['hwidDeviceLimit']
        device_limit = int(raw_device_limit) if raw_device_limit is not None else None
    except (KeyError, TypeError, ValueError):
        return None
    external = values['externalSquadUuid']
    if external is not None and not isinstance(external, str):
        return None
    return _WebhookPanelState(
        remnawave_id=remnawave_id,
        status=_normalize_status(values['status']),
        expire_at=expire_at,
        traffic_limit_bytes=traffic_limit_bytes,
        traffic_limit_strategy=strategy,
        squad_uuids=_extract_squad_uuids(squads),
        external_squad_uuid=external,
        last_traffic_reset_at=reset_at,
        updated_at=updated_at,
        used_traffic_bytes=used_traffic_bytes,
        device_limit=device_limit,
    )


def _timestamp_matches_mutation_window(
    updated_at: datetime,
    *,
    started_at: datetime | None,
    finished_at: datetime | None,
) -> bool:
    if started_at is None:
        return False
    start = _as_utc(started_at)
    finish = (
        _as_utc(finished_at)
        if finished_at is not None
        else min(
            _utc_now(),
            start + _MUTATION_ECHO_MAX_WINDOW,
        )
    )
    lower = start - _RESTORE_ECHO_TIMESTAMP_TOLERANCE
    upper = finish + _RESTORE_ECHO_TIMESTAMP_TOLERANCE
    return lower <= _as_utc(updated_at) <= upper


def _activation_echo_timestamp_matches(state: _WebhookPanelState, session: GraceAccessSession) -> bool:
    if session.activation_started_at is None:
        return False
    if session.activation_finished_at is None and session.state is not GraceSessionState.PENDING:
        # A durable intent proves an in-flight PENDING attempt, not that a
        # worker which already closed/moved the row ever reached Remnawave.
        return False
    bounded_finish = (
        min(
            _as_utc(session.activation_finished_at),
            _as_utc(session.activation_started_at) + _MUTATION_ECHO_MAX_WINDOW,
        )
        if session.activation_finished_at is not None
        else None
    )
    return _timestamp_matches_mutation_window(
        state.updated_at,
        started_at=session.activation_started_at,
        finished_at=bounded_finish,
    )


def _restore_state_timestamp_matches(state: _WebhookPanelState, session: GraceAccessSession) -> bool:
    return _timestamp_matches_mutation_window(
        state.updated_at,
        started_at=session.restore_started_at,
        finished_at=session.restore_finished_at,
    )


def _overlay_lifecycle_timestamp_matches(state: _WebhookPanelState, session: GraceAccessSession) -> bool:
    lower = _as_utc(session.started_at) - _RESTORE_ECHO_TIMESTAMP_TOLERANCE
    if session.state is GraceSessionState.COMPLETED:
        if session.completed_at is None:
            return False
        upper = _as_utc(session.completed_at) + _RESTORE_ECHO_TIMESTAMP_TOLERANCE
    else:
        upper = _utc_now() + _RESTORE_ECHO_TIMESTAMP_TOLERANCE
    return lower <= state.updated_at <= upper


def _webhook_device_matches_session(state: _WebhookPanelState, session: GraceAccessSession) -> bool:
    return state.device_limit == session.billing_before.device_limit


def _webhook_state_matches_overlay(
    state: _WebhookPanelState,
    session: GraceAccessSession,
    *,
    statuses: set[str],
) -> bool:
    overlay = session.overlay
    return (
        state.remnawave_id == session.remnawave_id
        and state.status in statuses
        and _datetimes_equal(state.expire_at, overlay.expire_at)
        and state.traffic_limit_bytes == overlay.traffic_limit_bytes
        and state.traffic_limit_strategy == overlay.traffic_limit_strategy
        and set(state.squad_uuids) == set(overlay.squad_uuids)
        and state.external_squad_uuid == overlay.external_squad_uuid
        and _reset_generations_equal(
            state.last_traffic_reset_at,
            overlay.expected_last_traffic_reset_at,
        )
        and _webhook_device_matches_session(state, session)
    )


def webhook_matches_overlay_event(
    payload: Mapping[str, Any],
    session: GraceAccessSession,
    event_name: str,
) -> bool:
    """Match a complete lifecycle echo of the exact Grace overlay."""
    expected_status = {'user.expired': {'expired'}, 'user.limited': {'limited'}}.get(event_name)
    if expected_status is None:
        return False
    state = _parse_webhook_panel_state(payload)
    if state is None or not _webhook_state_matches_overlay(state, session, statuses=expected_status):
        return False
    if event_name == 'user.expired':
        return abs((state.updated_at - _as_utc(session.overlay.expire_at)).total_seconds()) <= (
            _RESTORE_ECHO_TIMESTAMP_TOLERANCE.total_seconds()
        )
    return (
        _overlay_lifecycle_timestamp_matches(state, session)
        and session.overlay.traffic_limit_bytes > 0
        and state.used_traffic_bytes >= session.overlay.traffic_limit_bytes
    )


def webhook_matches_activation_modified(
    payload: Mapping[str, Any],
    session: GraceAccessSession,
) -> bool:
    """Match detach, NO_RESET or final-overlay user.modified echoes."""
    state = _parse_webhook_panel_state(payload)
    if state is None or not _activation_echo_timestamp_matches(state, session):
        return False
    if _webhook_state_matches_overlay(
        state,
        session,
        statuses={'active'},
    ):
        return True
    before = session.panel_before
    source_core = (
        state.remnawave_id == session.remnawave_id
        and state.status == _normalize_status(before.status)
        and _datetimes_equal(state.expire_at, before.expire_at)
        and state.traffic_limit_bytes == before.traffic_limit_bytes
        and set(state.squad_uuids) == set(before.squad_uuids)
        and state.external_squad_uuid == session.overlay.external_squad_uuid
        and _reset_generations_equal(
            state.last_traffic_reset_at,
            session.overlay.expected_last_traffic_reset_at,
        )
    )
    return (
        source_core
        and _webhook_device_matches_session(state, session)
        and state.traffic_limit_strategy
        in {
            before.traffic_limit_strategy,
            session.overlay.traffic_limit_strategy,
        }
    )


def webhook_matches_activation_enabled(
    payload: Mapping[str, Any],
    session: GraceAccessSession,
) -> bool:
    """Match only the final ACTIVE overlay emitted by Grace activation."""
    state = _parse_webhook_panel_state(payload)
    return bool(
        state is not None
        and _activation_echo_timestamp_matches(state, session)
        and _webhook_state_matches_overlay(state, session, statuses={'active'})
    )


def webhook_matches_limited_restore(
    payload: Mapping[str, Any],
    session: GraceAccessSession,
    *,
    event_name: str = 'user.modified',
) -> bool:
    """Match one exact phase of the LIMITED reverse transition."""
    if session.reason is not GraceReason.LIMITED or session.state not in {
        GraceSessionState.RESTORING,
        GraceSessionState.COMPLETED,
    }:
        return False
    state = _parse_webhook_panel_state(payload)
    if state is None or not _restore_state_timestamp_matches(state, session):
        return False
    expected_lifecycle_status = {
        'user.modified': {'active', 'limited'},
        'user.enabled': {'active'},
        'user.limited': {'limited'},
        'user.expired': {'expired'},
    }.get(event_name)
    if expected_lifecycle_status is None or state.status not in expected_lifecycle_status:
        return False
    if state.remnawave_id != session.remnawave_id or not _reset_generations_equal(
        state.last_traffic_reset_at,
        session.panel_before.last_traffic_reset_at,
    ):
        return False

    before = session.panel_before
    if before.expire_at is None:
        return False
    canonical_core = (
        _datetimes_equal(
            state.expire_at,
            before.expire_at,
        )
        and state.traffic_limit_bytes == before.traffic_limit_bytes
    )
    if not canonical_core or not _webhook_device_matches_session(state, session):
        return False

    no_reset = session.overlay.traffic_limit_strategy
    first_phase = (frozenset(session.overlay.squad_uuids), session.overlay.external_squad_uuid, no_reset)
    phases = {
        first_phase,
        (frozenset(before.squad_uuids), session.overlay.external_squad_uuid, no_reset),
        (frozenset(before.squad_uuids), before.external_squad_uuid, no_reset),
        (frozenset(before.squad_uuids), before.external_squad_uuid, before.traffic_limit_strategy),
    }
    phase = (frozenset(state.squad_uuids), state.external_squad_uuid, state.traffic_limit_strategy)
    if phase not in phases:
        return False
    if event_name == 'user.modified':
        return state.status == 'limited' or (state.status == 'active' and phase == first_phase)
    if event_name == 'user.enabled':
        return state.status == 'active' and phase == first_phase
    if event_name == 'user.limited':
        return state.status == 'limited' and phase == first_phase
    return state.status == 'expired' and phase == first_phase


def webhook_matches_billing_recovery(
    payload: Mapping[str, Any],
    billing: GraceBillingState,
) -> bool:
    """Allow user.enabled only for the exact fresh canonical billing target."""
    state = _parse_webhook_panel_state(payload)
    if (
        state is None
        or billing.remnawave_id is None
        or billing.end_at is None
        or billing.traffic_limit_strategy is None
    ):
        return False
    return (
        state.remnawave_id == billing.remnawave_id
        and state.status == 'active'
        and _datetimes_equal(state.expire_at, billing.end_at)
        and state.traffic_limit_bytes == billing.traffic_limit_bytes
        and state.traffic_limit_strategy == billing.traffic_limit_strategy
        and set(state.squad_uuids) == set(billing.squad_uuids)
        and state.external_squad_uuid == billing.external_squad_uuid
        and (billing.device_limit is None or state.device_limit == billing.device_limit)
    )


def webhook_matches_expired_restore(
    payload: Mapping[str, Any],
    session: GraceAccessSession,
    *,
    event_name: str = 'user.modified',
) -> bool:
    """Match only an ordered EXPIRED/DISABLED restore phase."""
    if not _session_can_match_expired_restore(session):
        return False
    if session.state is GraceSessionState.COMPLETED and session.completion_reason not in {
        GraceCompletionReason.TIMEOUT,
        GraceCompletionReason.DRAINED,
        GraceCompletionReason.REVOKED,
        GraceCompletionReason.CONFLICT,
    }:
        return False

    state = _parse_webhook_panel_state(payload)
    modified_statuses = {'disabled', 'expired'} if session.restore_force_disable else {'expired'}
    expected_statuses = {'user.modified': modified_statuses, 'user.expired': {'expired'}}.get(event_name)
    if (
        state is None
        or expected_statuses is None
        or state.status not in expected_statuses
        or state.remnawave_id != session.remnawave_id
        or not _restore_state_timestamp_matches(state, session)
        or not _reset_generations_equal(
            state.last_traffic_reset_at,
            session.panel_before.last_traffic_reset_at,
        )
        or not _webhook_device_matches_session(state, session)
    ):
        return False

    before = session.panel_before
    expiry_known = _datetimes_equal(state.expire_at, session.overlay.expire_at) or _datetimes_equal(
        state.expire_at,
        before.expire_at,
    )
    if not expiry_known:
        return False

    overlay_phase = (
        _datetimes_equal(state.expire_at, session.overlay.expire_at)
        and state.traffic_limit_bytes == session.overlay.traffic_limit_bytes
        and frozenset(state.squad_uuids) == frozenset(session.overlay.squad_uuids)
        and state.external_squad_uuid == session.overlay.external_squad_uuid
        and state.traffic_limit_strategy == session.overlay.traffic_limit_strategy
    )
    canonical_core = state.traffic_limit_bytes == before.traffic_limit_bytes and frozenset(
        state.squad_uuids
    ) == frozenset(before.squad_uuids)
    ordered_canonical_phase = canonical_core and (
        (
            state.external_squad_uuid == session.overlay.external_squad_uuid
            and state.traffic_limit_strategy == session.overlay.traffic_limit_strategy
        )
        or (
            state.external_squad_uuid == before.external_squad_uuid
            and state.traffic_limit_strategy == session.overlay.traffic_limit_strategy
        )
        or (
            state.external_squad_uuid == before.external_squad_uuid
            and state.traffic_limit_strategy == before.traffic_limit_strategy
        )
    )
    return overlay_phase or ordered_canonical_phase


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

    state = _parse_webhook_panel_state(payload)
    if state is None:
        return False

    return (
        state.status in {'active', 'limited'}
        and _datetimes_equal(state.expire_at, session.overlay.expire_at)
        and state.traffic_limit_bytes == max(1, session.traffic_reset_remaining_bytes)
        and state.traffic_limit_strategy == session.overlay.traffic_limit_strategy
        and set(state.squad_uuids) == set(session.overlay.squad_uuids)
        and state.external_squad_uuid == session.overlay.external_squad_uuid
        and _reset_generations_equal(
            state.last_traffic_reset_at,
            session.traffic_reset_previous_generation,
        )
        and state.used_traffic_bytes == session.traffic_reset_previous_used_bytes
        and _traffic_reset_device_matches(state, session)
    )


def webhook_matches_traffic_reset_enabled(
    payload: Mapping[str, Any],
    session: GraceAccessSession,
) -> bool:
    """Match LIMITED reset's pre-reset user.enabled snapshot."""
    if not _traffic_reset_webhook_identity_matches(payload, session):
        return False

    state = _parse_webhook_panel_state(payload)
    if state is None or session.reason is not GraceReason.LIMITED:
        return False
    return (
        state.status == 'active'
        and _datetimes_equal(state.expire_at, session.overlay.expire_at)
        and state.traffic_limit_bytes == max(1, session.traffic_reset_remaining_bytes or 0)
        and state.traffic_limit_strategy == session.overlay.traffic_limit_strategy
        and set(state.squad_uuids) == set(session.overlay.squad_uuids)
        and state.external_squad_uuid == session.overlay.external_squad_uuid
        and _reset_generations_equal(
            state.last_traffic_reset_at,
            session.traffic_reset_previous_generation,
        )
        and state.used_traffic_bytes == session.traffic_reset_previous_used_bytes
        and _traffic_reset_device_matches(state, session)
    )


def webhook_matches_traffic_reset_completed(
    payload: Mapping[str, Any],
    session: GraceAccessSession,
) -> bool:
    """Match the post-reset user.traffic_reset snapshot."""
    if not _traffic_reset_webhook_identity_matches(payload, session):
        return False
    state = _parse_webhook_panel_state(payload)
    if state is None:
        return False
    if session.traffic_reset_result_generation is not None:
        generation_matches = _reset_generations_equal(
            state.last_traffic_reset_at,
            session.traffic_reset_result_generation,
        )
    else:
        # The reset call is irreversible and its webhook may race the first
        # post-reset checkpoint. A new non-null generation plus the exact
        # persisted quota fence and zero usage is sufficient proof of that
        # in-flight intent; no second reset is inferred from it.
        generation_matches = (
            session.traffic_reset_target is not None
            and state.last_traffic_reset_at is not None
            and not _reset_generations_equal(
                state.last_traffic_reset_at,
                session.traffic_reset_previous_generation,
            )
        )
    return (
        state.status == 'active'
        and _datetimes_equal(state.expire_at, session.overlay.expire_at)
        and state.traffic_limit_bytes == max(1, session.traffic_reset_remaining_bytes or 0)
        and state.traffic_limit_strategy == session.overlay.traffic_limit_strategy
        and set(state.squad_uuids) == set(session.overlay.squad_uuids)
        and state.external_squad_uuid == session.overlay.external_squad_uuid
        and generation_matches
        and state.used_traffic_bytes == 0
        and _traffic_reset_device_matches(state, session)
    )


def _traffic_reset_device_matches(state: _WebhookPanelState, session: GraceAccessSession) -> bool:
    target = session.traffic_reset_target or session.billing_before
    return state.device_limit == target.device_limit


def _traffic_reset_webhook_identity_matches(
    payload: Mapping[str, Any],
    session: GraceAccessSession,
) -> bool:
    if (
        session.state
        not in {
            GraceSessionState.ACTIVE,
            GraceSessionState.RESTORING,
            GraceSessionState.COMPLETED,
        }
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


def _traffic_reset_echo_timestamp_matches(
    panel_updated_at: datetime | None,
    session: GraceAccessSession,
) -> bool:
    if panel_updated_at is None or session.traffic_reset_started_at is None:
        return False
    if session.state is GraceSessionState.COMPLETED and session.traffic_reset_finished_at is None:
        return False
    return _timestamp_matches_mutation_window(
        panel_updated_at,
        started_at=session.traffic_reset_started_at,
        finished_at=session.traffic_reset_finished_at,
    )


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
