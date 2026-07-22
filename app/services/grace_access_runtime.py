"""Production integration for restricted grace access.

The billing database remains canonical.  This module persists versioned
snapshots, applies a temporary Remnawave overlay, discovers recent incidents,
and reconciles open sessions.  It deliberately never changes a subscription's
billing dates/status and never resets used traffic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import (
    GraceAccessSessionModel,
    Subscription,
    SubscriptionStatus,
    User,
    UserStatus as DatabaseUserStatus,
)
from app.external.remnawave_api import UserStatus as PanelUserStatus
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
    GraceReconcileResult,
    GraceRestoreOutcome,
    GraceSessionState,
    GraceStartDecision,
    GraceStartResult,
    billing_is_eligible,
    build_incident_key,
    panel_is_safe_pending_source,
    panel_matches_overlay,
)


logger = structlog.get_logger(__name__)

_OPEN_STATES = (
    GraceSessionState.PENDING.value,
    GraceSessionState.ACTIVE.value,
    GraceSessionState.RESTORING.value,
)
_SNAPSHOT_VERSION = 2
_POSTGRES_LOCK_NAMESPACE = 1_196_572_995
_POSTGRES_GLOBAL_PANEL_LOCK_ID = 0


class GraceSnapshotError(ValueError):
    """A persisted snapshot is missing data required for a safe restore."""


class GracePanelError(RuntimeError):
    """Remnawave did not apply or verify a requested controlled state."""


class GraceAccessDeletionBlocked(RuntimeError):
    """A destructive operation was attempted before grace was restored."""

    def __init__(self, subscription_ids: Sequence[int]) -> None:
        self.subscription_ids = tuple(sorted({int(value) for value in subscription_ids}))
        joined = ', '.join(str(value) for value in self.subscription_ids)
        super().__init__(f'Open grace access must be finished before deletion (subscriptions: {joined})')


@dataclass(frozen=True)
class GracePanelUpdateLease:
    """Fresh billing state held under the same lock as an outbound panel write."""

    subscription: Subscription | None
    has_open_grace: bool
    db: AsyncSession

    @property
    def allowed(self) -> bool:
        return self.subscription is not None and not self.has_open_grace


class SQLAlchemyGraceSessionStore:
    """SQLAlchemy adapter for the persistence-neutral grace core."""

    def __init__(self, db: AsyncSession, *, subscription_id: int | None = None) -> None:
        self._db = db
        self._subscription_id = subscription_id

    async def get_open(self, subscription_id: int) -> GraceAccessSession | None:
        result = await self._db.execute(
            select(GraceAccessSessionModel)
            .execution_options(populate_existing=True)
            .where(
                GraceAccessSessionModel.subscription_id == subscription_id,
                GraceAccessSessionModel.state.in_(_OPEN_STATES),
            )
            .order_by(GraceAccessSessionModel.updated_at.desc())
            .limit(1)
        )
        model = result.scalar_one_or_none()
        return _model_to_session(model) if model else None

    async def get_by_incident(
        self,
        subscription_id: int,
        incident_key: str,
    ) -> GraceAccessSession | None:
        result = await self._db.execute(
            select(GraceAccessSessionModel)
            .execution_options(populate_existing=True)
            .where(
                GraceAccessSessionModel.subscription_id == subscription_id,
                GraceAccessSessionModel.incident_key == incident_key,
            )
        )
        model = result.scalar_one_or_none()
        return _model_to_session(model) if model else None

    async def create(self, session: GraceAccessSession) -> GraceAccessSession:
        model = _session_to_model(session)
        try:
            async with self._db.begin_nested():
                self._db.add(model)
                await self._db.flush()
            # PENDING must be durable before the external PATCH.  If the process
            # dies after this commit, reconciliation can safely finish or undo it.
            await self._db.commit()
            await _acquire_database_lock(self._db, session.subscription_id)
            refreshed = await self._db.execute(
                select(GraceAccessSessionModel)
                .execution_options(populate_existing=True)
                .where(GraceAccessSessionModel.id == session.id)
            )
            current_model = refreshed.scalar_one_or_none()
            if current_model is None:
                raise GraceSnapshotError(f'Grace session {session.id} disappeared after its durable create checkpoint')
            return _model_to_session(current_model)
        except IntegrityError:
            # Webhook and discovery worker may observe the same incident.  The
            # DB constraints decide the winner; the loser reloads that row.
            existing = await self.get_open(session.subscription_id)
            if existing:
                return existing
            existing = await self.get_by_incident(session.subscription_id, session.incident_key)
            if existing:
                return existing
            raise

    async def save(self, session: GraceAccessSession) -> GraceAccessSession:
        allowed_sources = {
            GraceSessionState.PENDING: (GraceSessionState.PENDING.value,),
            GraceSessionState.ACTIVE: (
                GraceSessionState.PENDING.value,
                GraceSessionState.ACTIVE.value,
            ),
            GraceSessionState.RESTORING: _OPEN_STATES,
            GraceSessionState.COMPLETED: _OPEN_STATES,
        }[session.state]
        statement = (
            update(GraceAccessSessionModel)
            .where(
                GraceAccessSessionModel.id == session.id,
                GraceAccessSessionModel.version == session.version,
                GraceAccessSessionModel.state.in_(allowed_sources),
            )
            .values(**_session_values(session), version=session.version + 1)
        )
        result = await self._db.execute(statement)
        if result.rowcount != 1:
            refreshed = await self._db.execute(
                select(GraceAccessSessionModel)
                .execution_options(populate_existing=True)
                .where(GraceAccessSessionModel.id == session.id)
            )
            current_model = refreshed.scalar_one_or_none()
            if current_model is None:
                raise GraceSnapshotError(f'Grace session {session.id} disappeared while it was being processed')
            # Optimistic CAS lost to another worker.  Returning the winner makes
            # retries idempotent and, critically, never regresses COMPLETED.
            return _model_to_session(current_model)

        saved = replace(session, version=session.version + 1)
        if session.state is GraceSessionState.RESTORING:
            # RESTORING is a durable checkpoint before the external restore
            # PATCH. It makes a crash after PATCH safely idempotent.
            await self._db.commit()
            await _acquire_database_lock(self._db, session.subscription_id)
            refreshed = await self._db.execute(
                select(GraceAccessSessionModel)
                .execution_options(populate_existing=True)
                .where(GraceAccessSessionModel.id == session.id)
            )
            current_model = refreshed.scalar_one_or_none()
            if current_model is None:
                raise GraceSnapshotError(f'Grace session {session.id} disappeared during restore checkpoint')
            return _model_to_session(current_model)
        return saved

    async def list_open(self, *, limit: int) -> Sequence[GraceAccessSession]:
        query = select(GraceAccessSessionModel).where(GraceAccessSessionModel.state.in_(_OPEN_STATES))
        if self._subscription_id is not None:
            query = query.where(GraceAccessSessionModel.subscription_id == self._subscription_id)
        result = await self._db.execute(
            query.execution_options(populate_existing=True)
            .order_by(
                GraceAccessSessionModel.grace_until.asc(),
                GraceAccessSessionModel.updated_at.asc(),
            )
            .limit(limit)
        )
        sessions: list[GraceAccessSession] = []
        for model in result.scalars().all():
            try:
                sessions.append(_model_to_session(model))
            except Exception as error:
                model.last_error = f'{type(error).__name__}: {error}'[:1000]
                logger.exception(
                    'Corrupt grace snapshot was left untouched',
                    grace_session_id=model.id,
                    subscription_id=model.subscription_id,
                )
        await self._db.flush()
        return sessions


class SQLAlchemyGraceBillingGateway:
    """Read canonical subscription data without changing it."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_subscription(self, subscription_id: int) -> GraceBillingState | None:
        result = await self._db.execute(
            select(Subscription)
            .execution_options(populate_existing=True)
            .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
            .where(Subscription.id == subscription_id)
        )
        subscription = result.scalar_one_or_none()
        if subscription is None or subscription.user is None:
            return None
        return _subscription_to_billing(subscription)


@dataclass(frozen=True, slots=True)
class _PanelTarget:
    status: PanelUserStatus
    expire_at: datetime
    traffic_limit_bytes: int
    squad_uuids: tuple[str, ...]
    external_squad_uuid: str | None
    device_limit: int | None = None


class RemnawaveGracePanelGateway:
    """Changes only fields controlled by the temporary overlay."""

    async def read_snapshot(self, remnawave_uuid: str) -> GracePanelSnapshot | None:
        from app.services.remnawave_service import remnawave_service

        async with remnawave_service.get_api_client() as api:
            panel_user = await api.get_user_by_uuid(remnawave_uuid)
        if panel_user is None:
            return None
        return _panel_user_to_snapshot(panel_user)

    async def apply_overlay(self, remnawave_uuid: str, overlay: GracePanelOverlay) -> None:
        from app.services.remnawave_service import remnawave_service

        async with remnawave_service.get_api_client() as api:
            # Detach an external squad in a standalone preflight PATCH.  The API
            # client may retry A039 without externalSquadUuid; doing this before
            # ACTIVE/expiry changes guarantees such a retry cannot accidentally
            # grant unrestricted access.
            detached = await api.update_user(
                uuid=remnawave_uuid,
                external_squad_uuid=overlay.external_squad_uuid,
            )
            if detached.external_squad_uuid != overlay.external_squad_uuid:
                verified_detach = await api.get_user_by_uuid(remnawave_uuid)
                if verified_detach is None or verified_detach.external_squad_uuid != overlay.external_squad_uuid:
                    raise GracePanelError('Remnawave did not detach the external squad; overlay was not granted')

            updated = await api.update_user(
                uuid=remnawave_uuid,
                status=PanelUserStatus.ACTIVE,
                expire_at=_as_utc(overlay.expire_at),
                traffic_limit_bytes=overlay.traffic_limit_bytes,
                active_internal_squads=list(overlay.squad_uuids),
            )
        if updated is None or not panel_matches_overlay(
            _panel_user_to_snapshot(updated),
            overlay,
            now=datetime.now(UTC),
        ):
            raise GracePanelError('Remnawave did not confirm the grace overlay')

    async def restore_snapshot(
        self,
        remnawave_uuid: str,
        snapshot: GracePanelSnapshot,
        expected_overlay: GracePanelOverlay,
    ) -> GraceRestoreOutcome:
        from app.services.remnawave_service import remnawave_service

        now = datetime.now(UTC)
        target = _build_restore_target(snapshot, now=now)

        async with remnawave_service.get_api_client() as api:
            current_user = await api.get_user_by_uuid(remnawave_uuid)
            if current_user is None:
                # A deleted panel user has no access left to revoke.
                return GraceRestoreOutcome.ALREADY_RESTORED

            current = _panel_user_to_snapshot(current_user)
            if _panel_matches_target(current, target):
                return GraceRestoreOutcome.ALREADY_RESTORED
            if not panel_matches_overlay(
                current,
                expected_overlay,
                now=now,
            ) and not panel_is_safe_pending_source(
                current,
                snapshot,
                expected_overlay,
            ):
                return GraceRestoreOutcome.CONFLICT

            updated = await api.update_user(
                uuid=remnawave_uuid,
                status=target.status,
                expire_at=target.expire_at,
                traffic_limit_bytes=target.traffic_limit_bytes,
                active_internal_squads=list(target.squad_uuids),
                external_squad_uuid=target.external_squad_uuid,
            )
            if updated is not None and _panel_matches_target(_panel_user_to_snapshot(updated), target):
                return GraceRestoreOutcome.RESTORED

            verified_user = await api.get_user_by_uuid(remnawave_uuid)
            if verified_user is not None and _panel_matches_target(
                _panel_user_to_snapshot(verified_user),
                target,
            ):
                return GraceRestoreOutcome.RESTORED
            if verified_user is not None:
                # A stale external-squad UUID may have been rejected while the
                # safe status/expiry restore succeeded. Do not retry forever or
                # overwrite a later manual correction; persist a terminal alert.
                return GraceRestoreOutcome.CONFLICT
        raise GracePanelError('Remnawave restore PATCH could not be verified')

    async def apply_billing_state(self, billing: GraceBillingState) -> None:
        from app.services.remnawave_service import remnawave_service

        if not billing.remnawave_uuid:
            raise GracePanelError('Canonical subscription has no Remnawave UUID')
        target = _build_billing_target(billing, now=datetime.now(UTC))
        kwargs: dict[str, Any] = {
            'uuid': billing.remnawave_uuid,
            'status': target.status,
            'expire_at': target.expire_at,
            'traffic_limit_bytes': target.traffic_limit_bytes,
            'active_internal_squads': list(target.squad_uuids),
            'external_squad_uuid': target.external_squad_uuid,
        }
        if target.device_limit is not None:
            kwargs['hwid_device_limit'] = target.device_limit

        async with remnawave_service.get_api_client() as api:
            updated = await api.update_user(**kwargs)
        if updated is None or not _panel_matches_target(_panel_user_to_snapshot(updated), target):
            raise GracePanelError('Remnawave did not confirm canonical billing state')


class _KeyedLocks:
    """Process-local part of the subscription operation lock."""

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._locks: dict[int, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, subscription_id: int):
        async with self._guard:
            lock, users = self._locks.get(subscription_id, (asyncio.Lock(), 0))
            self._locks[subscription_id] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            async with self._guard:
                current_lock, users = self._locks[subscription_id]
                if users <= 1 and not current_lock.locked():
                    self._locks.pop(subscription_id, None)
                else:
                    self._locks[subscription_id] = (current_lock, users - 1)


class GraceAccessRuntime:
    """Feature-mode facade and background reconciliation loop."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._locks = _KeyedLocks()
        self._mode = GraceAccessMode.DISABLED
        self._open_offset = 0
        self._candidate_offset = 0

    @property
    def mode(self) -> GraceAccessMode:
        return self._mode

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return

        # Keep ingress disabled until validation and the DB health check have
        # both succeeded.  A failed startup must never leave ACTIVE without a
        # reconciliation task.
        self._mode = GraceAccessMode.DISABLED
        try:
            requested_mode = GraceAccessMode.parse(settings.GRACE_ACCESS_MODE)
            if requested_mode is not GraceAccessMode.DISABLED:
                # Constructing the complete policy catches invalid/overflowing
                # duration values before webhook ingress or the worker starts.
                _build_policy()
            if requested_mode is GraceAccessMode.ACTIVE:
                _validate_active_configuration()
            open_count = await self.open_count()
        except Exception:
            self._mode = GraceAccessMode.DISABLED
            self._task = None
            self._stop_event.set()
            logger.critical('Grace startup failed; grace ingress remains disabled')
            raise

        if requested_mode in {GraceAccessMode.DISABLED, GraceAccessMode.OBSERVE} and open_count:
            logger.critical(
                'Grace runtime is non-mutating while open sessions still exist; use drain or restore-all',
                mode=requested_mode.value,
                open_sessions=open_count,
            )

        if requested_mode is GraceAccessMode.DISABLED:
            logger.info('Grace access is disabled', mode=requested_mode.value)
            return

        self._mode = requested_mode
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop(), name='grace-access-runtime')
        logger.info('Grace access runtime started', mode=self._mode.value, open_sessions=open_count)

    async def stop(self) -> None:
        # Close webhook ingress before stopping the reconciler.
        self._mode = GraceAccessMode.DISABLED
        task = self._task
        if task is None:
            return
        self._stop_event.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
        logger.info('Grace access runtime stopped')

    async def consider_candidate(
        self,
        subscription_id: int,
        reason: GraceReason,
        *,
        source: str,
    ) -> GraceStartResult | None:
        if self._mode in {GraceAccessMode.DISABLED, GraceAccessMode.DRAIN}:
            logger.debug(
                'Grace candidate ignored by runtime mode',
                subscription_id=subscription_id,
                reason=reason.value,
                mode=self._mode.value,
                source=source,
            )
            return None

        if self._mode is GraceAccessMode.OBSERVE:
            async with AsyncSessionLocal() as db:
                billing = await SQLAlchemyGraceBillingGateway(db).get_subscription(subscription_id)
            eligible = bool(billing and billing_is_eligible(billing, reason))
            logger.info(
                'Grace candidate observed',
                subscription_id=subscription_id,
                reason=reason.value,
                eligible=eligible,
                source=source,
            )
            return GraceStartResult(GraceStartDecision.NOT_ELIGIBLE if not eligible else GraceStartDecision.OBSERVED)

        try:
            processed_before = datetime.now(UTC)
            async with self._locks.hold(subscription_id):
                async with AsyncSessionLocal() as db:
                    await _acquire_database_lock(db, subscription_id)
                    billing = await SQLAlchemyGraceBillingGateway(db).get_subscription(subscription_id)
                    if billing is None:
                        return GraceStartResult(GraceStartDecision.NOT_ELIGIBLE)
                    try:
                        result = await _build_core(db, subscription_id=subscription_id).start_if_eligible(
                            billing,
                            reason,
                        )
                    except Exception:
                        # Overlay failures intentionally leave a durable PENDING
                        # row with last_error for the next reconciliation retry.
                        await db.commit()
                        raise
                    else:
                        await db.execute(
                            update(Subscription)
                            .where(
                                Subscription.id == subscription_id,
                                Subscription.grace_candidate_reason == reason.value,
                                or_(
                                    Subscription.grace_candidate_at.is_(None),
                                    Subscription.grace_candidate_at <= processed_before,
                                ),
                            )
                            .values(grace_candidate_reason=None, grace_candidate_at=None)
                        )
                        await db.commit()
            logger.info(
                'Grace candidate processed',
                subscription_id=subscription_id,
                reason=reason.value,
                decision=result.decision.value,
                source=source,
            )
            return result
        except Exception:
            logger.exception(
                'Grace candidate processing failed without affecting the billing event',
                subscription_id=subscription_id,
                reason=reason.value,
                source=source,
            )
            return None

    async def should_suppress_webhook(
        self,
        subscription_id: int,
        event_name: str,
        payload: Mapping[str, Any],
        *,
        db: AsyncSession | None = None,
    ) -> bool:
        if self._mode in (GraceAccessMode.DISABLED, GraceAccessMode.OBSERVE):
            # Non-mutating grace: оверлеев нет, эхо подавлять нечего — и не
            # тратим запрос к БД на каждый входящий webhook.
            return False
        try:
            if db is not None:
                core = _build_core(db, subscription_id=subscription_id)
                return await core.should_suppress_webhook(subscription_id, event_name, payload)
            async with AsyncSessionLocal() as own_db:
                core = _build_core(own_db, subscription_id=subscription_id)
                return await core.should_suppress_webhook(subscription_id, event_name, payload)
        except Exception:
            logger.exception(
                'Grace webhook guard failed',
                subscription_id=subscription_id,
                event_name=event_name,
            )
            # Generic status echoes are unsafe to apply while a persisted open
            # row exists, even if its JSON snapshot is corrupt.
            normalized_event = event_name.strip().lower()
            if normalized_event == 'user.disabled':
                return False
            if normalized_event in {'user.enabled', 'user.expired', 'user.limited'}:
                try:
                    if db is not None:
                        return subscription_id in await get_open_grace_subscription_ids(db)
                    async with AsyncSessionLocal() as own_db:
                        return subscription_id in await get_open_grace_subscription_ids(own_db)
                except Exception:
                    logger.exception('Grace webhook fallback guard also failed')
            return False

    async def run_once(self) -> None:
        if self._mode is GraceAccessMode.DISABLED:
            return
        if self._mode is GraceAccessMode.OBSERVE:
            await self._discover_candidates(observe_only=True)
            return

        await self._reconcile_open(drain=self._mode is GraceAccessMode.DRAIN)
        if self._mode is GraceAccessMode.ACTIVE:
            await self._discover_candidates(observe_only=False)

    async def force_restore_all(self) -> GraceReconcileResult:
        """Immediately restore every open session; used by the emergency CLI."""
        aggregate = GraceReconcileResult()
        while True:
            ids = await self._all_open_subscription_ids()
            if not ids:
                return aggregate
            progress = False
            for subscription_id in ids:
                try:
                    result = await self._process_open(
                        subscription_id,
                        drain=True,
                        force_restore=True,
                    )
                except Exception:
                    logger.exception(
                        'Emergency grace restore failed for subscription',
                        subscription_id=subscription_id,
                    )
                    result = GraceReconcileResult(inspected=1, errors=1)
                aggregate = _merge_reconcile_results(aggregate, result)
                if result.drained or result.paid or result.revoked or result.timed_out or result.conflicts:
                    progress = True
            if not progress:
                return aggregate

    async def open_count(self) -> int:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(func.count())
                .select_from(GraceAccessSessionModel)
                .where(GraceAccessSessionModel.state.in_(_OPEN_STATES))
            )
            return int(result.scalar_one())

    async def _run_loop(self) -> None:
        interval = settings.GRACE_ACCESS_RECONCILE_INTERVAL_SECONDS
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Grace runtime iteration failed; the next iteration will retry')

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def _discover_candidates(self, *, observe_only: bool) -> None:
        candidates = await self._recent_candidate_ids()
        if observe_only:
            for subscription_id, reason in candidates:
                await self.consider_candidate(subscription_id, reason, source='worker')
            return

        for subscription_id, reason in candidates:
            await self.consider_candidate(subscription_id, reason, source='worker')

    async def _recent_candidate_ids(self) -> list[tuple[int, GraceReason]]:
        now = datetime.now(UTC)
        cutoff = now - timedelta(minutes=settings.GRACE_ACCESS_CANDIDATE_LOOKBACK_MINUTES)
        batch_size = settings.GRACE_ACCESS_RECONCILE_BATCH_SIZE

        expired_recently = and_(
            Subscription.end_date >= cutoff,
            Subscription.end_date <= now,
            Subscription.status.in_((SubscriptionStatus.ACTIVE.value, SubscriptionStatus.EXPIRED.value)),
        )
        marked_candidate = and_(
            Subscription.grace_candidate_at >= cutoff,
            Subscription.grace_candidate_reason.in_((GraceReason.EXPIRED.value, GraceReason.LIMITED.value)),
        )

        async with AsyncSessionLocal() as db:
            query = (
                select(Subscription)
                .join(User, Subscription.user_id == User.id)
                .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
                .where(
                    User.status == DatabaseUserStatus.ACTIVE.value,
                    Subscription.is_trial.is_(False),
                    or_(expired_recently, marked_candidate),
                )
                .order_by(Subscription.updated_at.asc(), Subscription.id.asc())
            )
            subscriptions = (await db.execute(query)).scalars().all()

            existing_incidents: set[tuple[int, str]] = set()
            open_subscription_ids: set[int] = set()
            subscription_ids = [subscription.id for subscription in subscriptions]
            # SQLite has a comparatively small bind-parameter limit; chunks also
            # keep the PostgreSQL query plan predictable during a large expiry wave.
            for offset in range(0, len(subscription_ids), 500):
                chunk = subscription_ids[offset : offset + 500]
                if not chunk:
                    continue
                rows = await db.execute(
                    select(
                        GraceAccessSessionModel.subscription_id,
                        GraceAccessSessionModel.incident_key,
                        GraceAccessSessionModel.state,
                    ).where(GraceAccessSessionModel.subscription_id.in_(chunk))
                )
                for sub_id, key, state in rows.all():
                    existing_incidents.add((int(sub_id), str(key)))
                    if state in _OPEN_STATES:
                        open_subscription_ids.add(int(sub_id))

        if len(subscriptions) <= batch_size:
            self._candidate_offset = 0
            scan_subscriptions = subscriptions
        else:
            start = self._candidate_offset % len(subscriptions)
            scan_subscriptions = subscriptions[start:] + subscriptions[:start]
            self._candidate_offset = (start + batch_size) % len(subscriptions)

        candidates: list[tuple[int, GraceReason]] = []
        for subscription in scan_subscriptions:
            try:
                reason = (
                    GraceReason.LIMITED
                    if _normalize(subscription.status) == SubscriptionStatus.LIMITED.value
                    else GraceReason.EXPIRED
                )
                billing = _subscription_to_billing(subscription)
                if not billing.remnawave_uuid or not billing_is_eligible(billing, reason):
                    continue
                if subscription.id in open_subscription_ids:
                    continue
                if (
                    reason is GraceReason.EXPIRED
                    and (subscription.id, build_incident_key(billing, reason)) in existing_incidents
                ):
                    continue
                candidates.append((subscription.id, reason))
                if len(candidates) >= batch_size:
                    break
            except Exception:
                # One legacy/corrupt row must never prevent every other expired
                # customer from being processed during this iteration.
                logger.exception(
                    'Skipping invalid grace candidate',
                    subscription_id=subscription.id,
                )
        return candidates

    async def _reconcile_open(self, *, drain: bool) -> GraceReconcileResult:
        aggregate = GraceReconcileResult()
        for subscription_id in await self._open_subscription_ids():
            try:
                result = await self._process_open(subscription_id, drain=drain, force_restore=False)
            except Exception:
                logger.exception(
                    'Grace reconciliation failed before core processing',
                    subscription_id=subscription_id,
                )
                result = GraceReconcileResult(inspected=1, errors=1)
            aggregate = _merge_reconcile_results(aggregate, result)
        if aggregate.inspected:
            logger.info(
                'Grace reconciliation completed',
                mode=self._mode.value,
                inspected=aggregate.inspected,
                activated=aggregate.activated,
                paid=aggregate.paid,
                timed_out=aggregate.timed_out,
                drained=aggregate.drained,
                revoked=aggregate.revoked,
                conflicts=aggregate.conflicts,
                repaired=aggregate.repaired,
                errors=aggregate.errors,
            )
        return aggregate

    async def _open_subscription_ids(self) -> list[int]:
        all_ids = await self._all_open_subscription_ids()
        batch_size = settings.GRACE_ACCESS_RECONCILE_BATCH_SIZE
        if len(all_ids) <= batch_size:
            self._open_offset = 0
            return all_ids

        start = self._open_offset % len(all_ids)
        rotated = all_ids[start:] + all_ids[:start]
        self._open_offset = (start + batch_size) % len(all_ids)
        return rotated[:batch_size]

    async def _all_open_subscription_ids(self) -> list[int]:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(GraceAccessSessionModel.subscription_id)
                .where(GraceAccessSessionModel.state.in_(_OPEN_STATES))
                .order_by(GraceAccessSessionModel.grace_until.asc())
            )
            return [int(value) for value in result.scalars().all()]

    async def _process_open(
        self,
        subscription_id: int,
        *,
        drain: bool,
        force_restore: bool,
    ) -> GraceReconcileResult:
        async with self._locks.hold(subscription_id):
            async with AsyncSessionLocal() as db:
                await _acquire_database_lock(db, subscription_id)
                core = _build_core(db, subscription_id=subscription_id)
                result = (
                    await core.drain(limit=1, force_restore=force_restore) if drain else await core.reconcile(limit=1)
                )
                await db.commit()
                return result


async def get_open_grace_subscription_ids(db: AsyncSession) -> set[int]:
    """One-query guard shared by both directions of full synchronization."""
    if grace_access_runtime.mode in (GraceAccessMode.DISABLED, GraceAccessMode.OBSERVE):
        # Non-mutating grace: оверлеи не поддерживаются — синк идёт как до фичи,
        # без лишнего запроса на каждый webhook/цикл синхронизации.
        return set()
    result = await db.execute(
        select(GraceAccessSessionModel.subscription_id).where(GraceAccessSessionModel.state.in_(_OPEN_STATES))
    )
    return {int(value) for value in result.scalars().all()}


async def lock_grace_sensitive_panel_updates(
    db: AsyncSession,
    subscription_ids: Sequence[int],
) -> set[int]:
    """Serialize an outbound panel PATCH with grace creation/reconciliation.

    The returned set is read only after the transaction-scoped locks are held.
    Callers must keep the same transaction open through the Remnawave request
    and then commit or roll back, otherwise the check and PATCH are not atomic
    with respect to grace.
    """
    if grace_access_runtime.mode in (GraceAccessMode.DISABLED, GraceAccessMode.OBSERVE):
        # Non-mutating grace: локи не берём и оверлеи не защищаем — вызывающие
        # идут прямым панельным путём, как до фичи. Остаточные открытые сессии
        # в этих режимах отрапортованы CRITICAL-логом на старте runtime.
        return set()
    normalized_ids = tuple(sorted({int(value) for value in subscription_ids}))
    if not normalized_ids:
        return set()
    if db.get_bind().dialect.name == 'sqlite':
        await db.execute(
            update(Subscription).where(Subscription.id.in_(normalized_ids)).values(updated_at=Subscription.updated_at)
        )
    else:
        for subscription_id in normalized_ids:
            await _acquire_database_lock(db, subscription_id)
    result = await db.execute(
        select(GraceAccessSessionModel.subscription_id)
        .where(
            GraceAccessSessionModel.subscription_id.in_(normalized_ids),
            GraceAccessSessionModel.state.in_(_OPEN_STATES),
        )
        .distinct()
    )
    return {int(value) for value in result.scalars().all()}


async def apply_recovered_grace_update_locked(
    db: AsyncSession,
    api: Any,
    subscription_id: int,
    *,
    update_kwargs: Mapping[str, Any],
    source: str,
) -> tuple[bool, Any | None]:
    """Apply one canonical panel PATCH and finish a recovered grace session.

    The caller must already hold the subscription's grace-sensitive database
    lock and keep the transaction open until both the verified panel write and
    the session update are committed. ``false`` and ``observe`` remain strictly
    non-mutating; ``drain`` may finish an already-open session.
    """
    if grace_access_runtime.mode not in {GraceAccessMode.ACTIVE, GraceAccessMode.DRAIN}:
        return False, None

    core = _build_core(db, subscription_id=subscription_id)
    if not await core.payment_has_recovered(subscription_id):
        return False, None

    billing = await SQLAlchemyGraceBillingGateway(db).get_subscription(subscription_id)
    if billing is None or not billing.remnawave_uuid:
        raise GracePanelError('Recovered canonical subscription has no Remnawave UUID')

    target = _build_billing_target(billing, now=datetime.now(UTC))
    canonical_kwargs = dict(update_kwargs)
    canonical_kwargs.update(
        uuid=billing.remnawave_uuid,
        status=target.status,
        expire_at=target.expire_at,
        traffic_limit_bytes=target.traffic_limit_bytes,
        active_internal_squads=list(target.squad_uuids),
        external_squad_uuid=target.external_squad_uuid,
    )
    if target.device_limit is not None:
        canonical_kwargs['hwid_device_limit'] = target.device_limit

    updated = await api.update_user(**canonical_kwargs)
    if updated is None or not _panel_matches_target(_panel_user_to_snapshot(updated), target):
        raise GracePanelError('Remnawave did not confirm canonical billing state after renewal')

    completed = await core.complete_after_payment(
        subscription_id,
        apply_billing_state=False,
    )
    if not completed:
        raise GracePanelError('Recovered grace session changed before it could be completed')

    logger.info(
        'Grace access completed by the canonical renewal update',
        subscription_id=subscription_id,
        source=source,
    )
    return True, updated


@asynccontextmanager
async def grace_sensitive_panel_update(subscription_id: int):
    """Hold a grace lock and expose billing state read only after lock acquisition.

    Callers must build the Remnawave payload from ``lease.subscription`` rather
    than from an ORM object loaded before entering this context.  This makes a
    renewal that committed while a bulk sync was waiting win over that stale
    sync instead of being overwritten by it.
    """
    async with grace_access_runtime._locks.hold(subscription_id):
        async with AsyncSessionLocal() as guard_db:
            async with guard_db.begin():
                open_ids = await lock_grace_sensitive_panel_updates(guard_db, (subscription_id,))
                result = await guard_db.execute(
                    select(Subscription)
                    .options(
                        selectinload(Subscription.user),
                        selectinload(Subscription.tariff),
                    )
                    .execution_options(populate_existing=True)
                    .where(Subscription.id == subscription_id)
                )
                subscription = result.scalar_one_or_none()
                yield GracePanelUpdateLease(
                    subscription=subscription,
                    has_open_grace=subscription_id in open_ids,
                    db=guard_db,
                )


_GRACE_OWNED_UPDATE_FIELDS = frozenset(
    {
        'status',
        'expire_at',
        'traffic_limit_bytes',
        'traffic_limit_strategy',
        'active_internal_squads',
        'external_squad_uuid',
    }
)


async def update_panel_user_grace_safe(
    api: Any,
    subscription_id: int,
    **update_kwargs: Any,
) -> Any:
    """Apply a normal panel update without overwriting an open grace overlay.

    Metadata and device-limit changes are still allowed while grace is open.
    A real billing recovery completes grace immediately. Otherwise status,
    expiry, traffic and squad fields are deferred so the reconciler can keep
    the overlay or restore the newest canonical billing state safely.
    """
    if grace_access_runtime.mode in (GraceAccessMode.DISABLED, GraceAccessMode.OBSERVE):
        # Non-mutating grace: обычный панельный апдейт без guard-сессии и локов —
        # поведение и стоимость как до фичи. Оверлеи в этих режимах не защищаются:
        # рутинный синк приводит панель к каноническому биллингу (остаточные
        # открытые сессии отрапортованы CRITICAL-логом на старте).
        return await api.update_user(**update_kwargs)
    async with grace_sensitive_panel_update(subscription_id) as lease:
        if lease.subscription is None:
            raise GracePanelError(f'Subscription {subscription_id} disappeared before its Remnawave update')

        supplied_uuid = str(update_kwargs.get('uuid') or '')
        fresh_subscription = lease.subscription
        expected_uuid = (
            fresh_subscription.remnawave_uuid
            if settings.is_multi_tariff_enabled()
            else (fresh_subscription.user.remnawave_uuid if fresh_subscription.user else None)
        )
        if expected_uuid and supplied_uuid != str(expected_uuid):
            raise GracePanelError(f'Remnawave UUID changed before subscription {subscription_id} update')

        if not lease.has_open_grace:
            return await api.update_user(**update_kwargs)

        completed, updated = await apply_recovered_grace_update_locked(
            lease.db,
            api,
            subscription_id,
            update_kwargs=update_kwargs,
            source='grace_safe_panel_update',
        )
        if completed:
            return updated

        protected_present = _GRACE_OWNED_UPDATE_FIELDS.intersection(update_kwargs)
        if not protected_present:
            return await api.update_user(**update_kwargs)
        safe_kwargs = {key: value for key, value in update_kwargs.items() if key not in _GRACE_OWNED_UPDATE_FIELDS}
        logger.info(
            'Deferred grace-owned fields from routine Remnawave update',
            subscription_id=subscription_id,
            fields=sorted(protected_present),
        )
        if len(safe_kwargs) > 1:
            return await api.update_user(**safe_kwargs)

        current = await api.get_user_by_uuid(supplied_uuid)
        if current is None:
            raise GracePanelError(f'Remnawave user {supplied_uuid} disappeared while grace was open')
        return current


async def create_panel_user_grace_safe(
    api: Any,
    subscription_id: int,
    **create_kwargs: Any,
) -> Any:
    """Create a panel user only while the subscription cannot have an overlay."""
    if grace_access_runtime.mode in (GraceAccessMode.DISABLED, GraceAccessMode.OBSERVE):
        # Non-mutating grace: оверлеев не существует/не защищаются — создаём напрямую.
        return await api.create_user(**create_kwargs)
    async with grace_sensitive_panel_update(subscription_id) as lease:
        if lease.subscription is None:
            raise GracePanelError(f'Subscription {subscription_id} disappeared before Remnawave user creation')
        if lease.has_open_grace:
            raise GracePanelError(
                f'Remnawave user creation deferred while subscription {subscription_id} has open grace'
            )
        return await api.create_user(**create_kwargs)


@asynccontextmanager
async def grace_sensitive_global_panel_update():
    """Block all grace creation while one all-users panel mutation runs."""
    async with AsyncSessionLocal() as guard_db:
        async with guard_db.begin():
            if guard_db.get_bind().dialect.name == 'postgresql':
                await guard_db.execute(
                    text('SELECT pg_advisory_xact_lock(:namespace, :lock_id)'),
                    {
                        'namespace': _POSTGRES_LOCK_NAMESPACE,
                        'lock_id': _POSTGRES_GLOBAL_PANEL_LOCK_ID,
                    },
                )
            else:
                first_subscription_id = (await guard_db.execute(select(func.min(Subscription.id)))).scalar_one_or_none()
                if first_subscription_id is not None:
                    await guard_db.execute(
                        update(Subscription)
                        .where(Subscription.id == first_subscription_id)
                        .values(updated_at=Subscription.updated_at)
                    )

            open_count = (
                await guard_db.execute(
                    select(func.count())
                    .select_from(GraceAccessSessionModel)
                    .where(GraceAccessSessionModel.state.in_(_OPEN_STATES))
                )
            ).scalar_one()
            yield int(open_count) == 0


async def set_panel_user_enabled_state_grace_safe(
    api: Any,
    remnawave_uuid: str,
    *,
    enabled: bool,
    db: AsyncSession | None = None,
) -> Any:
    """Serialize an intentional enable/disable and its grace suppression marker.

    ``db`` — сессия вызывающего, который УЖЕ держит grace-локи затронутых
    подписок (пути удаления после ensure_no_open_grace_*). Advisory-локи
    PostgreSQL реентерабельны только в рамках одной сессии: вторая сессия здесь
    самодедлочилась бы об транзакционные локи первой. Suppression-маркеры в этом
    режиме коммитит транзакция вызывающего (откат удаления откатит и их — тогда
    и намеренного отключения не было).
    """
    if grace_access_runtime.mode in (GraceAccessMode.DISABLED, GraceAccessMode.OBSERVE):
        # Non-mutating grace: не трогаем ни БД, ни suppression-маркеры —
        # поведение панельного enable/disable как до фичи. Остаточные открытые
        # сессии в этих режимах уже отрапортованы CRITICAL-логом на старте.
        if enabled:
            return await api.enable_user(remnawave_uuid)
        return await api.disable_user(remnawave_uuid)

    if db is not None:
        action_result, deferred_disable_error = await _set_panel_user_enabled_state_locked(
            db, api, remnawave_uuid, enabled=enabled
        )
        if deferred_disable_error is not None:
            raise deferred_disable_error
        return action_result

    async with AsyncSessionLocal() as guard_db:
        async with guard_db.begin():
            action_result, deferred_disable_error = await _set_panel_user_enabled_state_locked(
                guard_db, api, remnawave_uuid, enabled=enabled
            )
    if deferred_disable_error is not None:
        raise deferred_disable_error
    return action_result


async def _set_panel_user_enabled_state_locked(
    guard_db: AsyncSession,
    api: Any,
    remnawave_uuid: str,
    *,
    enabled: bool,
) -> tuple[Any, BaseException | None]:
    action_result: Any = None
    deferred_disable_error: BaseException | None = None
    uuid_mapping_filter = (
        Subscription.remnawave_uuid == remnawave_uuid
        if settings.is_multi_tariff_enabled()
        else User.remnawave_uuid == remnawave_uuid
    )
    mapped_ids = {
        int(value)
        for value in (
            await guard_db.execute(
                select(Subscription.id).join(User, Subscription.user_id == User.id).where(uuid_mapping_filter)
            )
        ).scalars()
    }
    open_subscription_ids = {
        int(value)
        for value in (
            await guard_db.execute(
                select(GraceAccessSessionModel.subscription_id).where(
                    GraceAccessSessionModel.remnawave_uuid == remnawave_uuid,
                    GraceAccessSessionModel.state.in_(_OPEN_STATES),
                )
            )
        ).scalars()
    }
    mapped_ids.update(open_subscription_ids)

    for subscription_id in sorted(mapped_ids):
        await _acquire_database_lock(guard_db, subscription_id)

    subscriptions: list[Subscription] = []
    if mapped_ids:
        subscriptions = list(
            (
                await guard_db.execute(
                    select(Subscription)
                    .execution_options(populate_existing=True)
                    .where(Subscription.id.in_(sorted(mapped_ids)))
                )
            ).scalars()
        )

    now = datetime.now(UTC)
    enable_target_ids = set(open_subscription_ids)
    if enabled:
        enable_target_ids.update(
            subscription.id
            for subscription in subscriptions
            if subscription.actual_status in (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value)
        )
        if not enable_target_ids and subscriptions:
            latest = max(
                subscriptions,
                key=lambda subscription: (
                    _as_utc(subscription.end_date) if subscription.end_date else datetime.min.replace(tzinfo=UTC),
                    subscription.id,
                ),
            )
            enable_target_ids.add(latest.id)
    for subscription in subscriptions:
        if enabled:
            if subscription.id in enable_target_ids:
                subscription.grace_suppressed_until = None
        else:
            subscription.grace_suppressed_until = _as_utc(subscription.end_date) if subscription.end_date else now
    await guard_db.flush()

    try:
        if enabled:
            action_result = await api.enable_user(remnawave_uuid)
        else:
            action_result = await api.disable_user(remnawave_uuid)
    except asyncio.CancelledError as error:
        if enabled:
            raise
        deferred_disable_error = error
    except Exception as error:
        normalized_error = str(error).lower()
        already_in_state = (enabled and 'already enabled' in normalized_error) or (
            not enabled and 'already disabled' in normalized_error
        )
        if not already_in_state:
            if enabled:
                raise
            # The disable request may have reached Remnawave despite a
            # timeout. Commit suppression so grace can never re-enable
            # an intentionally revoked client, then report the error.
            deferred_disable_error = error
        else:
            current = await api.get_user_by_uuid(remnawave_uuid)
            if current is None:
                state_error = GracePanelError(f'Remnawave user {remnawave_uuid} disappeared during status update')
                if enabled:
                    raise state_error from error
                deferred_disable_error = state_error
            else:
                action_result = current

    return action_result, deferred_disable_error


async def ensure_no_open_grace_for_subscriptions(
    db: AsyncSession,
    subscription_ids: Sequence[int],
) -> None:
    """Fail before an irreversible panel/DB delete can orphan an overlay.

    The database trigger remains the last line of defence for unguarded bulk
    SQL.  User-facing destructive flows call this helper *before* touching the
    Remnawave user so they fail without creating a panel/database split.
    """
    normalized_ids = tuple(sorted({int(value) for value in subscription_ids}))
    if not normalized_ids:
        return
    # Keep the guard held until the caller commits/rolls back so a PENDING row
    # cannot appear after this check but before an irreversible panel delete.
    # PostgreSQL uses the exact worker advisory-lock namespace. SQLite has no
    # advisory locks, so an idempotent write obtains its database RESERVED lock;
    # candidate activation always persists PENDING before touching the panel.
    if db.get_bind().dialect.name == 'sqlite':
        await db.execute(
            update(Subscription).where(Subscription.id.in_(normalized_ids)).values(updated_at=Subscription.updated_at)
        )
    else:
        for subscription_id in normalized_ids:
            await _acquire_database_lock(db, subscription_id)
    result = await db.execute(
        select(GraceAccessSessionModel.subscription_id)
        .where(
            GraceAccessSessionModel.subscription_id.in_(normalized_ids),
            GraceAccessSessionModel.state.in_(_OPEN_STATES),
        )
        .distinct()
    )
    blocked = tuple(int(value) for value in result.scalars().all())
    if blocked:
        logger.warning(
            'Destructive operation blocked by open grace access',
            subscription_ids=blocked,
        )
        # The guard deliberately acquires transaction-scoped locks. Nothing
        # destructive has happened yet, so release them before handing the
        # expected rejection back to a request/bulk loop.
        await db.rollback()
        raise GraceAccessDeletionBlocked(blocked)


async def ensure_no_open_grace_for_user(db: AsyncSession, user_id: int) -> None:
    """User-level version of the pre-delete guard."""
    await ensure_no_open_grace_for_users(db, (user_id,))


async def ensure_no_open_grace_for_users(db: AsyncSession, user_ids: Sequence[int]) -> None:
    """Acquire every affected subscription lock in deterministic order."""
    normalized_user_ids = tuple(sorted({int(value) for value in user_ids}))
    if not normalized_user_ids:
        return
    # Lock the owner rows before enumerating subscriptions. PostgreSQL FK
    # inserts take a conflicting key-share lock; SQLite's no-op write obtains
    # the database write lock. Thus a new subscription cannot slip into a full
    # user delete/account merge after the enumeration.
    if db.get_bind().dialect.name == 'sqlite':
        await db.execute(update(User).where(User.id.in_(normalized_user_ids)).values(id=User.id))
    else:
        await db.execute(
            select(User.id).where(User.id.in_(normalized_user_ids)).order_by(User.id.asc()).with_for_update()
        )
    result = await db.execute(select(Subscription.id).where(Subscription.user_id.in_(normalized_user_ids)))
    await ensure_no_open_grace_for_subscriptions(db, tuple(int(value) for value in result.scalars().all()))


def _build_core(db: AsyncSession, *, subscription_id: int | None = None) -> GraceAccessService:
    return GraceAccessService(
        store=SQLAlchemyGraceSessionStore(db, subscription_id=subscription_id),
        panel=RemnawaveGracePanelGateway(),
        billing=SQLAlchemyGraceBillingGateway(db),
        policy=_build_policy(),
    )


def _build_policy() -> GraceAccessPolicy:
    gib = 1024**3
    return GraceAccessPolicy(
        duration=timedelta(hours=settings.GRACE_ACCESS_DURATION_HOURS),
        expired_squad_uuid=settings.GRACE_ACCESS_EXPIRED_SQUAD_UUID.strip(),
        limited_squad_uuid=settings.GRACE_ACCESS_LIMITED_SQUAD_UUID.strip(),
        expired_traffic_bytes=settings.GRACE_ACCESS_EXPIRED_TRAFFIC_GB * gib,
        limited_traffic_bytes=settings.GRACE_ACCESS_LIMITED_TRAFFIC_GB * gib,
        reconcile_batch_size=settings.GRACE_ACCESS_RECONCILE_BATCH_SIZE,
    )


def _validate_active_configuration() -> None:
    if settings.GRACE_ACCESS_EXPIRED_TRAFFIC_GB < 1:
        raise ValueError('GRACE_ACCESS_EXPIRED_TRAFFIC_GB must be at least 1 when GRACE_ACCESS_MODE=true')
    if settings.GRACE_ACCESS_LIMITED_TRAFFIC_GB < 1:
        raise ValueError('GRACE_ACCESS_LIMITED_TRAFFIC_GB must be at least 1 when GRACE_ACCESS_MODE=true')
    for label, raw_uuid in (
        ('GRACE_ACCESS_EXPIRED_SQUAD_UUID', settings.GRACE_ACCESS_EXPIRED_SQUAD_UUID),
        ('GRACE_ACCESS_LIMITED_SQUAD_UUID', settings.GRACE_ACCESS_LIMITED_SQUAD_UUID),
    ):
        if not raw_uuid.strip():
            raise ValueError(f'{label} is required when GRACE_ACCESS_MODE=true')
        try:
            UUID(raw_uuid.strip())
        except ValueError as error:
            raise ValueError(f'{label} must contain a valid UUID') from error


async def _acquire_database_lock(db: AsyncSession, subscription_id: int) -> None:
    bind = db.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    await db.execute(
        text('SELECT pg_advisory_xact_lock_shared(:namespace, :lock_id)'),
        {
            'namespace': _POSTGRES_LOCK_NAMESPACE,
            'lock_id': _POSTGRES_GLOBAL_PANEL_LOCK_ID,
        },
    )
    await db.execute(
        text('SELECT pg_advisory_xact_lock(:namespace, :subscription_id)'),
        {'namespace': _POSTGRES_LOCK_NAMESPACE, 'subscription_id': subscription_id},
    )


def _subscription_to_billing(subscription: Subscription) -> GraceBillingState:
    user = subscription.user
    tariff = subscription.tariff
    remnawave_uuid = subscription.remnawave_uuid if settings.is_multi_tariff_enabled() else user.remnawave_uuid
    traffic_limit_gb = max(0, int(subscription.traffic_limit_gb or 0))
    traffic_used_gb = max(0.0, float(subscription.traffic_used_gb or 0.0))
    return GraceBillingState(
        subscription_id=subscription.id,
        remnawave_uuid=remnawave_uuid,
        status=subscription.actual_status,
        end_at=_as_utc(subscription.end_date) if subscription.end_date else None,
        traffic_limit_bytes=traffic_limit_gb * 1024**3,
        used_traffic_bytes=int(traffic_used_gb * 1024**3),
        device_limit=subscription.device_limit,
        squad_uuids=_string_tuple(subscription.connected_squads),
        external_squad_uuid=(tariff.external_squad_uuid if tariff else None),
        is_trial=bool(subscription.is_trial or subscription.status == SubscriptionStatus.TRIAL.value),
        is_daily=bool(tariff and tariff.is_daily),
        is_free_tariff=bool(tariff and tariff.is_free),
        user_status=user.status,
        grace_suppressed_until=(
            _as_utc(subscription.grace_suppressed_until) if subscription.grace_suppressed_until else None
        ),
    )


def _panel_user_to_snapshot(panel_user: Any) -> GracePanelSnapshot:
    return GracePanelSnapshot(
        remnawave_uuid=str(panel_user.uuid),
        status=_normalize(panel_user.status),
        expire_at=_as_utc(panel_user.expire_at) if panel_user.expire_at else None,
        traffic_limit_bytes=int(panel_user.traffic_limit_bytes or 0),
        used_traffic_bytes=int(panel_user.used_traffic_bytes or 0),
        squad_uuids=_extract_panel_squads(panel_user.active_internal_squads),
        external_squad_uuid=panel_user.external_squad_uuid,
        traffic_is_known=panel_user.user_traffic is not None,
        last_traffic_reset_at=(_as_utc(panel_user.last_traffic_reset_at) if panel_user.last_traffic_reset_at else None),
    )


def _build_restore_target(snapshot: GracePanelSnapshot, *, now: datetime) -> _PanelTarget:
    status = _normalize(snapshot.status)
    expire_at = _as_utc(snapshot.expire_at) if snapshot.expire_at else now
    if status in {'expired', 'disabled'} or expire_at <= now:
        return _PanelTarget(
            status=PanelUserStatus.DISABLED,
            expire_at=max(expire_at, now + timedelta(minutes=1)),
            traffic_limit_bytes=snapshot.traffic_limit_bytes,
            squad_uuids=snapshot.squad_uuids,
            external_squad_uuid=snapshot.external_squad_uuid,
        )
    if status == 'limited':
        panel_status = PanelUserStatus.LIMITED
    else:
        panel_status = PanelUserStatus.ACTIVE
    return _PanelTarget(
        status=panel_status,
        expire_at=expire_at,
        traffic_limit_bytes=snapshot.traffic_limit_bytes,
        squad_uuids=snapshot.squad_uuids,
        external_squad_uuid=snapshot.external_squad_uuid,
    )


def _build_billing_target(billing: GraceBillingState, *, now: datetime) -> _PanelTarget:
    status = _normalize(billing.status)
    user_active = _normalize(billing.user_status) == DatabaseUserStatus.ACTIVE.value
    expire_at = _as_utc(billing.end_at) if billing.end_at else now
    if user_active and status in {'active', 'trial'} and expire_at > now:
        panel_status = PanelUserStatus.ACTIVE
        safe_expire_at = expire_at
    elif user_active and status == 'limited' and expire_at > now:
        panel_status = PanelUserStatus.LIMITED
        safe_expire_at = expire_at
    else:
        panel_status = PanelUserStatus.DISABLED
        safe_expire_at = max(expire_at, now + timedelta(minutes=1))
    return _PanelTarget(
        status=panel_status,
        expire_at=safe_expire_at,
        traffic_limit_bytes=billing.traffic_limit_bytes,
        squad_uuids=billing.squad_uuids,
        external_squad_uuid=billing.external_squad_uuid,
        device_limit=billing.device_limit,
    )


def _panel_matches_target(snapshot: GracePanelSnapshot, target: _PanelTarget) -> bool:
    actual_status = _normalize(snapshot.status)
    expected_status = _normalize(target.status)
    if expected_status == 'disabled':
        status_matches = actual_status in {'disabled', 'expired'}
        expiry_matches = True
    else:
        status_matches = actual_status == expected_status
        expiry_matches = bool(
            snapshot.expire_at and abs((_as_utc(snapshot.expire_at) - _as_utc(target.expire_at)).total_seconds()) <= 2
        )
    return (
        status_matches
        and expiry_matches
        and snapshot.traffic_limit_bytes == target.traffic_limit_bytes
        and set(snapshot.squad_uuids) == set(target.squad_uuids)
        and snapshot.external_squad_uuid == target.external_squad_uuid
    )


def _extract_panel_squads(raw_squads: Any) -> tuple[str, ...]:
    if not isinstance(raw_squads, list):
        return ()
    values: list[str] = []
    for raw_squad in raw_squads:
        value = raw_squad.get('uuid') if isinstance(raw_squad, dict) else raw_squad
        if value is not None and str(value) not in values:
            values.append(str(value))
    return tuple(values)


def _session_to_model(session: GraceAccessSession) -> GraceAccessSessionModel:
    model = GraceAccessSessionModel(id=session.id)
    _copy_session_to_model(session, model)
    model.version = session.version
    return model


def _copy_session_to_model(
    session: GraceAccessSession,
    model: GraceAccessSessionModel,
) -> None:
    for key, value in _session_values(session).items():
        setattr(model, key, value)


def _session_values(session: GraceAccessSession) -> dict[str, Any]:
    return {
        'subscription_id': session.subscription_id,
        'remnawave_uuid': session.remnawave_uuid,
        'reason': session.reason.value,
        'incident_key': session.incident_key,
        'state': session.state.value,
        'snapshot_version': _SNAPSHOT_VERSION,
        'billing_before': _billing_to_json(session.billing_before),
        'panel_before': _panel_to_json(session.panel_before),
        'overlay': _overlay_to_json(session.overlay),
        'started_at': _as_utc(session.started_at),
        'grace_until': _as_utc(session.grace_until),
        'updated_at': _as_utc(session.updated_at),
        'completion_reason': session.completion_reason.value if session.completion_reason else None,
        'completed_at': _as_utc(session.completed_at) if session.completed_at else None,
        'last_error': session.last_error,
    }


def _model_to_session(model: GraceAccessSessionModel) -> GraceAccessSession:
    if model.snapshot_version != _SNAPSHOT_VERSION:
        raise GraceSnapshotError(
            f'Unsupported grace snapshot version {model.snapshot_version}; expected {_SNAPSHOT_VERSION}'
        )
    return GraceAccessSession(
        id=model.id,
        subscription_id=model.subscription_id,
        remnawave_uuid=model.remnawave_uuid,
        reason=GraceReason(model.reason),
        incident_key=model.incident_key,
        state=GraceSessionState(model.state),
        billing_before=_billing_from_json(model.billing_before),
        panel_before=_panel_from_json(model.panel_before),
        overlay=_overlay_from_json(model.overlay),
        started_at=_as_utc(model.started_at),
        grace_until=_as_utc(model.grace_until),
        updated_at=_as_utc(model.updated_at),
        completion_reason=(GraceCompletionReason(model.completion_reason) if model.completion_reason else None),
        completed_at=_as_utc(model.completed_at) if model.completed_at else None,
        last_error=model.last_error,
        version=model.version,
    )


def _billing_to_json(value: GraceBillingState) -> dict[str, Any]:
    return {
        'subscription_id': value.subscription_id,
        'remnawave_uuid': value.remnawave_uuid,
        'status': value.status,
        'end_at': _datetime_to_json(value.end_at),
        'traffic_limit_bytes': value.traffic_limit_bytes,
        'used_traffic_bytes': value.used_traffic_bytes,
        'device_limit': value.device_limit,
        'squad_uuids': list(value.squad_uuids),
        'external_squad_uuid': value.external_squad_uuid,
        'is_trial': value.is_trial,
        'is_daily': value.is_daily,
        'is_free_tariff': value.is_free_tariff,
        'user_status': value.user_status,
        'grace_suppressed_until': _datetime_to_json(value.grace_suppressed_until),
    }


def _billing_from_json(raw: Any) -> GraceBillingState:
    data = _mapping(raw, 'billing_before')
    return GraceBillingState(
        subscription_id=_integer(data, 'subscription_id'),
        remnawave_uuid=_optional_string(data.get('remnawave_uuid')),
        status=_string(data, 'status'),
        end_at=_datetime_from_json(data.get('end_at')),
        traffic_limit_bytes=_integer(data, 'traffic_limit_bytes'),
        used_traffic_bytes=_integer(data, 'used_traffic_bytes'),
        device_limit=_optional_integer(data.get('device_limit')),
        squad_uuids=_string_tuple(data.get('squad_uuids')),
        external_squad_uuid=_optional_string(data.get('external_squad_uuid')),
        is_trial=bool(data.get('is_trial', False)),
        is_daily=bool(data.get('is_daily', False)),
        is_free_tariff=bool(data.get('is_free_tariff', False)),
        user_status=str(data.get('user_status', 'active')),
        grace_suppressed_until=_datetime_from_json(data.get('grace_suppressed_until')),
    )


def _panel_to_json(value: GracePanelSnapshot) -> dict[str, Any]:
    return {
        'remnawave_uuid': value.remnawave_uuid,
        'status': value.status,
        'expire_at': _datetime_to_json(value.expire_at),
        'traffic_limit_bytes': value.traffic_limit_bytes,
        'used_traffic_bytes': value.used_traffic_bytes,
        'squad_uuids': list(value.squad_uuids),
        'external_squad_uuid': value.external_squad_uuid,
        'traffic_is_known': value.traffic_is_known,
        'last_traffic_reset_at': _datetime_to_json(value.last_traffic_reset_at),
    }


def _panel_from_json(raw: Any) -> GracePanelSnapshot:
    data = _mapping(raw, 'panel_before')
    return GracePanelSnapshot(
        remnawave_uuid=_string(data, 'remnawave_uuid'),
        status=_string(data, 'status'),
        expire_at=_datetime_from_json(data.get('expire_at')),
        traffic_limit_bytes=_integer(data, 'traffic_limit_bytes'),
        used_traffic_bytes=_integer(data, 'used_traffic_bytes'),
        squad_uuids=_string_tuple(data.get('squad_uuids')),
        external_squad_uuid=_optional_string(data.get('external_squad_uuid')),
        traffic_is_known=bool(data.get('traffic_is_known', True)),
        last_traffic_reset_at=_datetime_from_json(data.get('last_traffic_reset_at')),
    )


def _overlay_to_json(value: GracePanelOverlay) -> dict[str, Any]:
    return {
        'status': value.status,
        'expire_at': _datetime_to_json(value.expire_at),
        'traffic_limit_bytes': value.traffic_limit_bytes,
        'squad_uuids': list(value.squad_uuids),
        'external_squad_uuid': value.external_squad_uuid,
    }


def _overlay_from_json(raw: Any) -> GracePanelOverlay:
    data = _mapping(raw, 'overlay')
    expire_at = _datetime_from_json(data.get('expire_at'))
    if expire_at is None:
        raise GraceSnapshotError('overlay.expire_at is required')
    return GracePanelOverlay(
        status=_string(data, 'status'),
        expire_at=expire_at,
        traffic_limit_bytes=_integer(data, 'traffic_limit_bytes'),
        squad_uuids=_string_tuple(data.get('squad_uuids')),
        external_squad_uuid=_optional_string(data.get('external_squad_uuid')),
    )


def _mapping(raw: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(raw, dict):
        raise GraceSnapshotError(f'{label} must be a JSON object')
    return raw


def _string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise GraceSnapshotError(f'{key} must be a non-empty string')
    return value


def _integer(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise GraceSnapshotError(f'{key} must be an integer')
    return value


def _optional_integer(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise GraceSnapshotError('Optional integer value is invalid')
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise GraceSnapshotError('Optional string value is invalid')
    return value


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise GraceSnapshotError('Squad UUIDs must be a list')
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise GraceSnapshotError('Every squad UUID must be a non-empty string')
        if item not in result:
            result.append(item)
    return tuple(result)


def _datetime_to_json(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value else None


def _datetime_from_json(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise GraceSnapshotError('Datetime snapshot value must be an ISO-8601 string')
    try:
        return _as_utc(datetime.fromisoformat(value.replace('Z', '+00:00')))
    except ValueError as error:
        raise GraceSnapshotError(f'Invalid datetime snapshot value: {value}') from error


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalize(value: object) -> str:
    raw = getattr(value, 'value', value)
    return str(raw).strip().lower().rsplit('.', maxsplit=1)[-1]


def _merge_reconcile_results(
    left: GraceReconcileResult,
    right: GraceReconcileResult,
) -> GraceReconcileResult:
    return GraceReconcileResult(
        inspected=left.inspected + right.inspected,
        activated=left.activated + right.activated,
        paid=left.paid + right.paid,
        timed_out=left.timed_out + right.timed_out,
        drained=left.drained + right.drained,
        revoked=left.revoked + right.revoked,
        conflicts=left.conflicts + right.conflicts,
        repaired=left.repaired + right.repaired,
        unchanged=left.unchanged + right.unchanged,
        errors=left.errors + right.errors,
    )


grace_access_runtime = GraceAccessRuntime()
