"""Production integration for restricted grace access.

The billing database remains canonical.  This module persists versioned
snapshots, applies a temporary Remnawave overlay, discovers recent incidents,
and reconciles open sessions.  Billing remains canonical; the only coordinated
accounting mutation here is an explicitly configured tariff-switch traffic
reset while a Grace overlay is open.
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
from app.external.remnawave_api import (
    RemnaWaveInvalidUserIdError,
    TrafficLimitStrategy,
    UserStatus as PanelUserStatus,
    coerce_panel_user_id,
)
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
    GraceReconcileResult,
    GraceRestoreOutcome,
    GraceSessionState,
    GraceStartDecision,
    GraceStartResult,
    GraceTrafficResetOutcome,
    GraceTrafficResetResult,
    billing_is_eligible,
    build_incident_key,
    build_tariff_rebase_lineage_key,
    panel_is_safe_pending_source,
    panel_matches_overlay,
    traffic_reset_billing_matches_target,
)


logger = structlog.get_logger(__name__)

_OPEN_STATES = (
    GraceSessionState.PENDING.value,
    GraceSessionState.ACTIVE.value,
    GraceSessionState.RESTORING.value,
)
_SNAPSHOT_VERSION = 4
# Version 3 stores the numeric Remnawave 3.0.0 identity.  Version 2 rows are
# still read: the backfill adds the numeric key *next to* the historical uuid
# instead of replacing it, and a session that predates the panel upgrade must
# stay reconcilable.  Refusing v2 here would make `_model_to_session` raise for
# every such row, `list_open` would drop them from the batch, and their overlay
# would never be rolled back — a permanently open door with no error report.
_SUPPORTED_SNAPSHOT_VERSIONS = frozenset({2, 3, _SNAPSHOT_VERSION})
_TRAFFIC_LIMIT_STRATEGIES = frozenset({'NO_RESET', 'DAY', 'WEEK', 'MONTH', 'MONTH_ROLLING'})
_POSTGRES_LOCK_NAMESPACE = 1_196_572_995
_POSTGRES_GLOBAL_PANEL_LOCK_ID = 0
_GRACE_EXPIRE_AT_SAFETY_MARGIN = timedelta(seconds=60)


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


async def _repair_missing_panel_id(db: AsyncSession, model: GraceAccessSessionModel) -> bool:
    """Дозаполнить `remnawave_id` сессии из тех же источников, что и бэкфилл.

    Сессия с пустой колонкой нечитаема: `_model_to_session` бросает
    `GraceSnapshotError`. Такая строка бессмертна — закрыть её некому, новый
    грейс для этой подписки не откроется из-за уникального индекса на открытую
    сессию, а фоновой разбор пишет ошибку каждый цикл. Между тем ответ обычно
    лежит рядом: подписка (или, в однотарифном, её владелец) уже связаны —
    бэкфилом или самим ботом после него.

    Возвращает True, если идентичность восстановлена.
    """
    if model.remnawave_id is not None:
        return False

    panel_id = (
        await db.execute(select(Subscription.remnawave_id).where(Subscription.id == model.subscription_id))
    ).scalar_one_or_none()

    if panel_id is None and not settings.is_multi_tariff_enabled():
        # В однотарифном идентичность канонически живёт на пользователе.
        panel_id = (
            await db.execute(
                select(User.remnawave_id)
                .join(Subscription, Subscription.user_id == User.id)
                .where(Subscription.id == model.subscription_id)
            )
        ).scalar_one_or_none()

    if panel_id is None:
        return False

    model.remnawave_id = int(panel_id)
    model.last_error = None
    logger.info(
        'Идентичность grace-сессии восстановлена из подписки',
        grace_session_id=model.id,
        subscription_id=model.subscription_id,
        remnawave_id=int(panel_id),
    )
    return True


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
        if model is None:
            return None
        if model.remnawave_id is None:
            await _repair_missing_panel_id(self._db, model)
        return _model_to_session(model)

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
        if model is not None:
            if model.remnawave_id is None:
                await _repair_missing_panel_id(self._db, model)
            return _model_to_session(model)

        # Rebased LIMITED sessions keep their immutable primary incident key
        # and persist additional dedupe aliases inside the existing versioned
        # JSON snapshot. Legacy rows have no such metadata, so their lineage is
        # also derived from the immutable pre-Grace snapshot. Scanning one
        # subscription's small Grace history keeps this portable across
        # PostgreSQL and SQLite JSON implementations.
        alias_result = await self._db.execute(
            select(GraceAccessSessionModel)
            .execution_options(populate_existing=True)
            .where(GraceAccessSessionModel.subscription_id == subscription_id)
            .order_by(GraceAccessSessionModel.updated_at.desc())
        )
        for alias_model in alias_result.scalars().all():
            try:
                if alias_model.remnawave_id is None:
                    await _repair_missing_panel_id(self._db, alias_model)
                session = _model_to_session(alias_model)
            except Exception:
                logger.exception(
                    'Corrupt grace snapshot was ignored during incident alias lookup',
                    grace_session_id=alias_model.id,
                    subscription_id=subscription_id,
                )
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
            if incident_key in session.incident_aliases or incident_key == legacy_lineage_key:
                return session
        return None

    async def list_recent_completed(
        self,
        subscription_id: int,
        *,
        limit: int = 8,
    ) -> Sequence[GraceAccessSession]:
        query = select(GraceAccessSessionModel).where(
            GraceAccessSessionModel.subscription_id == subscription_id,
            GraceAccessSessionModel.state == GraceSessionState.COMPLETED.value,
        )
        result = await self._db.execute(
            query.execution_options(populate_existing=True)
            .order_by(
                GraceAccessSessionModel.completed_at.desc(),
                GraceAccessSessionModel.updated_at.desc(),
            )
            .limit(limit)
        )
        sessions: list[GraceAccessSession] = []
        for model in result.scalars().all():
            try:
                sessions.append(_model_to_session(model))
            except Exception:
                logger.exception(
                    'Corrupt completed grace snapshot was ignored by the echo guard',
                    grace_session_id=model.id,
                    subscription_id=subscription_id,
                )
        return sessions

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
            # COMPLETED -> COMPLETED is reserved for metadata-only LIMITED
            # lineage advancement after a post-timeout tariff switch.  CAS still
            # prevents stale workers from changing a terminal winner.
            GraceSessionState.COMPLETED: (*_OPEN_STATES, GraceSessionState.COMPLETED.value),
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
        durable_external_checkpoint = session.state is GraceSessionState.RESTORING or (
            session.state is GraceSessionState.ACTIVE
            and (session.allow_recovery_enabled_webhook or session.traffic_reset_target is not None)
        )
        if durable_external_checkpoint:
            # RESTORING and the narrowly marked tariff-recovery transition are
            # durable checkpoints before an external PATCH.  The latter must be
            # visible so a concurrent user.enabled webhook is not suppressed as
            # an ordinary Grace echo.
            await self._db.commit()
            await _acquire_database_lock(self._db, session.subscription_id)
            refreshed = await self._db.execute(
                select(GraceAccessSessionModel)
                .execution_options(populate_existing=True)
                .where(GraceAccessSessionModel.id == session.id)
            )
            current_model = refreshed.scalar_one_or_none()
            if current_model is None:
                raise GraceSnapshotError(f'Grace session {session.id} disappeared during external checkpoint')
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
                if model.remnawave_id is None:
                    await _repair_missing_panel_id(self._db, model)
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
    """Read canonical billing and confirm one verified reset recovery."""

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

    async def mark_active_after_traffic_reset(
        self,
        expected: GraceBillingState,
    ) -> GraceBillingState | None:
        result = await self._db.execute(
            select(Subscription)
            .execution_options(populate_existing=True)
            .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
            .where(Subscription.id == expected.subscription_id)
        )
        subscription = result.scalar_one_or_none()
        if subscription is None or subscription.user is None:
            return None
        current = _subscription_to_billing(subscription)
        if (
            expected.end_at is None
            or _as_utc(expected.end_at) <= datetime.now(UTC)
            or not traffic_reset_billing_matches_target(
                current,
                expected,
                GraceReason.LIMITED,
            )
        ):
            return None
        if subscription.status == SubscriptionStatus.LIMITED.value:
            subscription.status = SubscriptionStatus.ACTIVE.value
            subscription.grace_candidate_reason = None
            subscription.grace_candidate_at = None
            subscription.updated_at = datetime.now(UTC)
            await self._db.flush((subscription,))
        return _subscription_to_billing(subscription)


@dataclass(frozen=True, slots=True)
class _PanelTarget:
    status: PanelUserStatus
    expire_at: datetime | None
    traffic_limit_bytes: int
    squad_uuids: tuple[str, ...]
    external_squad_uuid: str | None
    traffic_limit_strategy: str | None = None
    device_limit: int | None = None
    write_expire_at: bool = True
    write_traffic_limit_strategy: bool = True


class RemnawaveGracePanelGateway:
    """Changes only fields controlled by the temporary overlay."""

    async def read_snapshot(self, remnawave_id: int) -> GracePanelSnapshot | None:
        from app.services.remnawave_service import remnawave_service

        # An unusable local identifier raises RemnaWaveInvalidUserIdError from the
        # client boundary instead of returning None: that is a broken link in our
        # database, not a deleted panel user, and must never be answered by
        # "nothing left to restore".
        async with remnawave_service.get_api_client() as api:
            panel_user = await api.get_user_by_id(remnawave_id)
        if panel_user is None:
            return None
        return _panel_user_to_snapshot(panel_user)

    async def apply_overlay(self, remnawave_id: int, overlay: GracePanelOverlay) -> None:
        from app.services.remnawave_service import remnawave_service

        async with remnawave_service.get_api_client() as api:
            # Detach an external squad in a standalone preflight PATCH.  The API
            # client may retry A039 without externalSquadUuid; doing this before
            # ACTIVE/expiry changes guarantees such a retry cannot accidentally
            # grant unrestricted access.
            detached = await api.update_user(
                user_id=remnawave_id,
                external_squad_uuid=overlay.external_squad_uuid,
            )
            if detached.external_squad_uuid != overlay.external_squad_uuid:
                verified_detach = await api.get_user_by_id(remnawave_id)
                if verified_detach is None or verified_detach.external_squad_uuid != overlay.external_squad_uuid:
                    raise GracePanelError('Remnawave did not detach the external squad; overlay was not granted')

            detached_snapshot = _panel_user_to_snapshot(detached)
            if not _reset_generations_equal(
                detached_snapshot.last_traffic_reset_at,
                overlay.expected_last_traffic_reset_at,
            ):
                raise GracePanelTransitionConflict(
                    'Remnawave reset generation changed while detaching the external squad'
                )

            strategy_updated = await api.update_user(
                user_id=remnawave_id,
                traffic_limit_strategy=TrafficLimitStrategy.NO_RESET,
            )
            if strategy_updated is None:
                strategy_updated = await api.get_user_by_id(remnawave_id)
            if strategy_updated is None:
                raise GracePanelError('Remnawave user disappeared while disabling the automatic traffic reset')
            strategy_snapshot = _panel_user_to_snapshot(strategy_updated)
            if (
                strategy_snapshot.traffic_limit_strategy != TrafficLimitStrategy.NO_RESET.value
                or not _reset_generations_equal(
                    strategy_snapshot.last_traffic_reset_at,
                    overlay.expected_last_traffic_reset_at,
                )
            ):
                raise GracePanelError('Remnawave did not confirm NO_RESET before granting Grace')

            updated = await api.update_user(
                **_serialize_grace_panel_patch(
                    remnawave_id,
                    status=PanelUserStatus.ACTIVE,
                    expire_at=overlay.expire_at,
                    write_expire_at=True,
                    base_kwargs={
                        'traffic_limit_bytes': overlay.traffic_limit_bytes,
                        'active_internal_squads': list(overlay.squad_uuids),
                        'traffic_limit_strategy': TrafficLimitStrategy.NO_RESET,
                    },
                )
            )
        updated_snapshot = _panel_user_to_snapshot(updated) if updated is not None else None
        if (
            updated_snapshot is None
            or not panel_matches_overlay(updated_snapshot, overlay, now=datetime.now(UTC))
            or not _reset_generations_equal(
                updated_snapshot.last_traffic_reset_at,
                overlay.expected_last_traffic_reset_at,
            )
        ):
            raise GracePanelError('Remnawave did not confirm the grace overlay')

    async def restore_snapshot(
        self,
        remnawave_id: int,
        snapshot: GracePanelSnapshot,
        expected_overlay: GracePanelOverlay,
        *,
        force_disable: bool = False,
    ) -> GraceRestoreOutcome:
        from app.services.remnawave_service import remnawave_service

        now = datetime.now(UTC)
        target = _build_restore_target(snapshot, now=now)

        async with remnawave_service.get_api_client() as api:
            # Only an explicit 404 reaches this as None.  A malformed local
            # identifier raises instead, so a data fault can never be mistaken
            # for "the panel user is gone, nothing to restore".
            current_user = await api.get_user_by_id(remnawave_id)
            if current_user is None:
                # A deleted panel user has no access left to revoke.
                return GraceRestoreOutcome.ALREADY_RESTORED

            current = _panel_user_to_snapshot(current_user)
            if target.status is PanelUserStatus.EXPIRED:
                return await _restore_expired_target(
                    api,
                    remnawave_id=remnawave_id,
                    target=target,
                    snapshot=snapshot,
                    expected_overlay=expected_overlay,
                    current_user=current_user,
                    now=now,
                    force_disable=force_disable,
                )
            if target.status is PanelUserStatus.DISABLED and not target.write_expire_at:
                target = replace(target, expire_at=current.expire_at)
            if target.status is PanelUserStatus.DISABLED:
                already_matches = _panel_user_matches_restored_disabled_target(current_user, target)
            else:
                already_matches = _panel_matches_target(current, target)
            if already_matches:
                return GraceRestoreOutcome.ALREADY_RESTORED
            disabled_overlay_intermediate = (
                target.status is PanelUserStatus.DISABLED
                and _panel_matches_disabled_overlay_intermediate(current, expected_overlay)
            )
            if target.status is PanelUserStatus.LIMITED:
                if not _limited_transition_source_is_safe(
                    current,
                    target,
                    expected_overlay,
                    now=now,
                ):
                    return GraceRestoreOutcome.CONFLICT
                updated = await _apply_limited_target(
                    api,
                    remnawave_id=remnawave_id,
                    target=target,
                    expected_overlay=expected_overlay,
                    current_user=current_user,
                )
                return GraceRestoreOutcome.RESTORED if updated is not None else GraceRestoreOutcome.CONFLICT
            if (
                not panel_matches_overlay(
                    current,
                    expected_overlay,
                    now=now,
                )
                and not panel_is_safe_pending_source(
                    current,
                    snapshot,
                    expected_overlay,
                )
                and not disabled_overlay_intermediate
            ):
                return GraceRestoreOutcome.CONFLICT

            if target.status is PanelUserStatus.DISABLED:
                updated = await _apply_restore_disabled_target(
                    api,
                    remnawave_id=remnawave_id,
                    target=target,
                    current_user=current_user,
                )
                return GraceRestoreOutcome.RESTORED if updated is not None else GraceRestoreOutcome.CONFLICT

            updated = await _restore_target_in_phases(
                api,
                remnawave_id=remnawave_id,
                target=target,
                current_user=current_user,
            )
            if updated is not None and _panel_matches_target(_panel_user_to_snapshot(updated), target):
                return GraceRestoreOutcome.RESTORED

            verified_user = await api.get_user_by_id(remnawave_id)
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

    async def revoke_missing_billing(
        self,
        remnawave_id: int,
        *,
        expected_overlay: GracePanelOverlay,
    ) -> None:
        """Fail closed from an exact reset-owned state when billing vanished."""
        from app.services.remnawave_service import remnawave_service

        async with remnawave_service.get_api_client() as api:
            current_user = await api.get_user_by_id(remnawave_id)
            if current_user is None:
                return
            current = _panel_user_to_snapshot(current_user)
            if _panel_matches_inactive_overlay(current, expected_overlay):
                return
            if not panel_matches_overlay(
                current,
                expected_overlay,
                now=datetime.now(UTC),
            ):
                raise GracePanelTransitionConflict('Remnawave changed outside the deleted tariff reset checkpoint')

            disabled_user = await api.disable_user(remnawave_id)
            if disabled_user is None:
                disabled_user = await api.get_user_by_id(remnawave_id)
            if disabled_user is None:
                return
            if not _panel_matches_inactive_overlay(
                _panel_user_to_snapshot(disabled_user),
                expected_overlay,
            ):
                raise GracePanelError('Remnawave did not confirm revocation after canonical billing disappeared')

    async def prepare_tariff_rebase(
        self,
        billing: GraceBillingState,
        *,
        expected_overlay: GracePanelOverlay,
        expected_last_traffic_reset_at: datetime | None,
    ) -> GracePanelSnapshot | None:
        """Verify the exact overlay and apply only tariff metadata it does not own.

        Traffic quota, expiry and routing remain byte-for-byte the original
        Grace grant.  The device limit is safe to refresh immediately because
        Grace never owns it; all fields are verified again after the PATCH.
        """
        from app.services.remnawave_service import remnawave_service

        if not billing.remnawave_id:
            return None
        target = _build_billing_target(billing, now=datetime.now(UTC))

        async with remnawave_service.get_api_client() as api:
            current_user = await api.get_user_by_id(billing.remnawave_id)
            if current_user is None:
                return None
            current = _panel_user_to_snapshot(current_user)
            if not panel_matches_overlay(
                current,
                expected_overlay,
                now=datetime.now(UTC),
            ) or not _reset_generations_equal(
                current.last_traffic_reset_at,
                expected_last_traffic_reset_at,
            ):
                return None

            verified_user = current_user
            if not _panel_user_matches_device_limit(current_user, target):
                if target.device_limit is None:
                    return None
                verified_user = await api.update_user(
                    user_id=billing.remnawave_id,
                    hwid_device_limit=target.device_limit,
                )
                if verified_user is None:
                    verified_user = await api.get_user_by_id(billing.remnawave_id)
                if verified_user is None:
                    return None

            verified = _panel_user_to_snapshot(verified_user)
            if not panel_matches_overlay(
                verified,
                expected_overlay,
                now=datetime.now(UTC),
            ):
                return None
            if not _reset_generations_equal(
                verified.last_traffic_reset_at,
                expected_last_traffic_reset_at,
            ):
                return None
            if not _panel_user_matches_device_limit(verified_user, target):
                return None
            return verified

    async def apply_tariff_switch_traffic_reset(
        self,
        billing: GraceBillingState,
        *,
        reason: GraceReason,
        expected_overlay: GracePanelOverlay,
        expected_last_traffic_reset_at: datetime | None,
        remaining_grace_bytes: int,
    ) -> GraceTrafficResetResult:
        """Reset usage without turning an absolute Grace limit into free quota.

        The quota fence is applied before the irreversible reset.  A durable
        core marker makes every phase retryable: an already changed reset
        generation is accepted only from that exact fence or the exact final
        canonical target.
        """
        from app.services.remnawave_service import remnawave_service

        if not billing.remnawave_id:
            raise GracePanelTransitionConflict('Canonical tariff reset target has no Remnawave user id')
        now = datetime.now(UTC)
        keeps_grace = not (
            reason is GraceReason.LIMITED and billing.end_at is not None and _as_utc(billing.end_at) > now
        )
        fence_limit = max(1, remaining_grace_bytes)
        reset_overlay = replace(
            expected_overlay,
            traffic_limit_bytes=fence_limit,
        )
        recovered_billing = replace(
            billing,
            status='active',
            used_traffic_bytes=0,
        )
        recovered_target = _build_billing_target(recovered_billing, now=now)
        expired_target = _build_billing_target(billing, now=now)

        async with remnawave_service.get_api_client() as api:
            current_user = await api.get_user_by_id(billing.remnawave_id)
            if current_user is None:
                raise GracePanelTransitionConflict('Remnawave user disappeared during the configured tariff reset')
            current = _panel_user_to_snapshot(current_user)
            old_generation = _reset_generations_equal(
                current.last_traffic_reset_at,
                expected_last_traffic_reset_at,
            )

            if (
                not old_generation
                and not keeps_grace
                and _panel_user_matches_target(
                    current_user,
                    recovered_target,
                )
            ):
                return GraceTrafficResetResult(
                    GraceTrafficResetOutcome.RECOVERED,
                    current,
                )

            exhausted_target = replace(
                expired_target,
                status=PanelUserStatus.DISABLED,
                expire_at=current.expire_at,
                write_expire_at=False,
            )
            if (
                not old_generation
                and keeps_grace
                and remaining_grace_bytes == 0
                and _panel_user_matches_restored_disabled_target(current_user, exhausted_target)
            ):
                return GraceTrafficResetResult(
                    GraceTrafficResetOutcome.EXHAUSTED,
                    current,
                )

            fence_matches = panel_matches_overlay(
                current,
                reset_overlay,
                now=now,
            )
            if old_generation:
                source_matches = (
                    panel_matches_overlay(
                        current,
                        expected_overlay,
                        now=now,
                    )
                    or fence_matches
                )
                if not source_matches:
                    raise GracePanelTransitionConflict('Remnawave changed before the configured tariff reset fence')
                if not fence_matches or not _panel_user_matches_device_limit(
                    current_user,
                    recovered_target,
                ):
                    fence_kwargs: dict[str, Any] = {
                        'user_id': billing.remnawave_id,
                        'traffic_limit_bytes': reset_overlay.traffic_limit_bytes,
                        'active_internal_squads': list(reset_overlay.squad_uuids),
                        'external_squad_uuid': reset_overlay.external_squad_uuid,
                    }
                    if recovered_target.device_limit is not None:
                        fence_kwargs['hwid_device_limit'] = recovered_target.device_limit
                    current_user = await api.update_user(**fence_kwargs)
                    if current_user is None:
                        current_user = await api.get_user_by_id(billing.remnawave_id)
                    if current_user is None:
                        raise GracePanelError('Remnawave user disappeared after the tariff reset fence')
                    current = _panel_user_to_snapshot(current_user)
                    if (
                        not panel_matches_overlay(current, reset_overlay, now=now)
                        or not _reset_generations_equal(
                            current.last_traffic_reset_at,
                            expected_last_traffic_reset_at,
                        )
                        or not _panel_user_matches_device_limit(
                            current_user,
                            recovered_target,
                        )
                    ):
                        raise GracePanelError('Remnawave did not confirm the tariff reset quota fence')

                reset_user = await api.reset_user_traffic(billing.remnawave_id)
                if reset_user is None:
                    reset_user = await api.get_user_by_id(billing.remnawave_id)
                if reset_user is None:
                    raise GracePanelError('Remnawave user disappeared after the configured traffic reset')
                current_user = reset_user
                current = _panel_user_to_snapshot(reset_user)
                if (
                    not current.traffic_is_known
                    or current.used_traffic_bytes != 0
                    or _reset_generations_equal(
                        current.last_traffic_reset_at,
                        expected_last_traffic_reset_at,
                    )
                ):
                    raise GracePanelError('Remnawave did not confirm a new zero-usage reset generation')
            elif not fence_matches:
                raise GracePanelTransitionConflict(
                    'Remnawave reset generation changed outside the persisted quota fence'
                )

            if not keeps_grace:
                if not _panel_user_matches_target(current_user, recovered_target):
                    current_user = await api.update_user(
                        **_serialize_panel_target(
                            billing.remnawave_id,
                            recovered_target,
                        )
                    )
                    if current_user is None:
                        current_user = await api.get_user_by_id(billing.remnawave_id)
                if current_user is None or not _panel_user_matches_target(
                    current_user,
                    recovered_target,
                ):
                    raise GracePanelError('Remnawave did not confirm the active tariff after traffic reset')
                return GraceTrafficResetResult(
                    GraceTrafficResetOutcome.RECOVERED,
                    _panel_user_to_snapshot(current_user),
                )

            if remaining_grace_bytes == 0:
                if not _panel_user_matches_restored_disabled_target(
                    current_user,
                    exhausted_target,
                ):
                    current_user = await _apply_restore_disabled_target(
                        api,
                        remnawave_id=billing.remnawave_id,
                        target=exhausted_target,
                        current_user=current_user,
                    )
                if current_user is None or not _panel_user_matches_restored_disabled_target(
                    current_user,
                    exhausted_target,
                ):
                    raise GracePanelError('Remnawave did not confirm the exhausted Grace reset target')
                return GraceTrafficResetResult(
                    GraceTrafficResetOutcome.EXHAUSTED,
                    _panel_user_to_snapshot(current_user),
                )

            if not panel_matches_overlay(current, reset_overlay, now=now) or not _panel_user_matches_device_limit(
                current_user, recovered_target
            ):
                overlay_kwargs: dict[str, Any] = _serialize_grace_panel_patch(
                    billing.remnawave_id,
                    status=PanelUserStatus.ACTIVE,
                    expire_at=reset_overlay.expire_at,
                    write_expire_at=True,
                    base_kwargs={
                        'traffic_limit_bytes': reset_overlay.traffic_limit_bytes,
                        'active_internal_squads': list(reset_overlay.squad_uuids),
                        'external_squad_uuid': reset_overlay.external_squad_uuid,
                    },
                )
                if recovered_target.device_limit is not None:
                    overlay_kwargs['hwid_device_limit'] = recovered_target.device_limit
                current_user = await api.update_user(**overlay_kwargs)
                if current_user is None:
                    current_user = await api.get_user_by_id(billing.remnawave_id)
                if current_user is None:
                    raise GracePanelError('Remnawave user disappeared while continuing Grace after reset')
                current = _panel_user_to_snapshot(current_user)
            if (
                not panel_matches_overlay(current, reset_overlay, now=now)
                or _reset_generations_equal(
                    current.last_traffic_reset_at,
                    expected_last_traffic_reset_at,
                )
                or not _panel_user_matches_device_limit(current_user, recovered_target)
            ):
                raise GracePanelError('Remnawave did not confirm the continued Grace overlay after reset')
            return GraceTrafficResetResult(
                GraceTrafficResetOutcome.CONTINUED,
                current,
                overlay=reset_overlay,
            )

    async def apply_billing_state(
        self,
        billing: GraceBillingState,
        *,
        expected_overlay: GracePanelOverlay,
        expected_restored_snapshot: GracePanelSnapshot | None = None,
        require_overlay_source: bool = False,
        expected_last_traffic_reset_at: datetime | None = None,
    ) -> None:
        from app.services.remnawave_service import remnawave_service

        if not billing.remnawave_id:
            raise GracePanelError('Canonical subscription has no Remnawave user id')
        now = datetime.now(UTC)
        target = _build_billing_target(billing, now=now)
        explicit_disabled = _billing_requires_explicit_disabled(billing)

        async with remnawave_service.get_api_client() as api:
            if target.status is PanelUserStatus.LIMITED:
                current_user = await api.get_user_by_id(billing.remnawave_id)
                if current_user is None:
                    raise GracePanelTransitionConflict('Canonical Remnawave user disappeared during LIMITED restore')
                current = _panel_user_to_snapshot(current_user)
                if _panel_matches_target(current, target):
                    if _panel_user_matches_device_limit(current_user, target):
                        return
                    updated_device = await api.update_user(
                        user_id=billing.remnawave_id,
                        hwid_device_limit=target.device_limit,
                    )
                    if updated_device is None:
                        updated_device = await api.get_user_by_id(billing.remnawave_id)
                    if updated_device is not None and _panel_user_matches_target(updated_device, target):
                        return
                    raise GracePanelTransitionConflict('Remnawave did not confirm canonical LIMITED device limit')
                source_is_safe = _limited_transition_source_is_safe(
                    current,
                    target,
                    expected_overlay,
                    now=now,
                )
                if expected_restored_snapshot is not None:
                    source_is_safe = source_is_safe or _limited_source_matches_previous_restore(
                        current,
                        expected_restored_snapshot,
                        expected_overlay,
                        now=now,
                    )
                if not source_is_safe:
                    raise GracePanelTransitionConflict(
                        'Remnawave changed outside grace; canonical LIMITED state was not applied'
                    )
                updated = await _apply_limited_target(
                    api,
                    remnawave_id=billing.remnawave_id,
                    target=target,
                    expected_overlay=expected_overlay,
                    current_user=current_user,
                )
            elif target.status is PanelUserStatus.DISABLED:
                current_user = await api.get_user_by_id(billing.remnawave_id)
                if current_user is None:
                    raise GracePanelError('Canonical Remnawave user disappeared during DISABLED restore')
                if not target.write_expire_at:
                    target = replace(
                        target,
                        expire_at=_panel_user_to_snapshot(current_user).expire_at,
                    )
                current_matches = (
                    _panel_user_matches_disabled_target_exact(current_user, target)
                    if explicit_disabled
                    else _panel_user_matches_restored_disabled_target(current_user, target)
                )
                if current_matches:
                    return
                if explicit_disabled:
                    updated = await _apply_canonical_disabled_target(
                        api,
                        remnawave_id=billing.remnawave_id,
                        target=target,
                        current_user=current_user,
                    )
                else:
                    updated = await _apply_restore_disabled_target(
                        api,
                        remnawave_id=billing.remnawave_id,
                        target=target,
                        current_user=current_user,
                    )
            else:
                if require_overlay_source:
                    current_user = await api.get_user_by_id(billing.remnawave_id)
                    if current_user is None:
                        raise GracePanelTransitionConflict(
                            'Canonical Remnawave user disappeared during tariff recovery'
                        )
                    current = _panel_user_to_snapshot(current_user)
                    if _panel_matches_target(current, target):
                        if _panel_user_matches_device_limit(current_user, target):
                            return
                        updated_device = await api.update_user(
                            user_id=billing.remnawave_id,
                            hwid_device_limit=target.device_limit,
                        )
                        if updated_device is None:
                            updated_device = await api.get_user_by_id(billing.remnawave_id)
                        if updated_device is not None and _panel_user_matches_target(
                            updated_device,
                            target,
                        ):
                            return
                        raise GracePanelTransitionConflict('Remnawave did not confirm recovered tariff device limit')
                    if not panel_matches_overlay(
                        current,
                        expected_overlay,
                        now=now,
                    ) or not _reset_generations_equal(
                        current.last_traffic_reset_at,
                        expected_last_traffic_reset_at,
                    ):
                        raise GracePanelTransitionConflict('Remnawave changed outside grace during tariff recovery')
                updated = await api.update_user(**_serialize_panel_target(billing.remnawave_id, target))
        if target.status is PanelUserStatus.DISABLED:
            target_matches = updated is not None and (
                _panel_user_matches_disabled_target_exact(updated, target)
                if explicit_disabled
                else _panel_user_matches_restored_disabled_target(updated, target)
            )
        else:
            target_matches = updated is not None and _panel_user_matches_target(updated, target)
        if not target_matches:
            if target.status is PanelUserStatus.LIMITED:
                raise GracePanelTransitionConflict('Remnawave changed while canonical LIMITED state was being applied')
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
            eligible = bool(billing and billing_is_eligible(billing, reason, _build_policy()))
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
        policy = _build_policy()

        expired_recently = and_(
            Subscription.end_date >= cutoff,
            Subscription.end_date <= now,
            Subscription.status.in_(
                (
                    SubscriptionStatus.ACTIVE.value,
                    SubscriptionStatus.TRIAL.value,
                    SubscriptionStatus.EXPIRED.value,
                )
            ),
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
                if not billing.remnawave_id or not billing_is_eligible(billing, reason, policy):
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
    if billing is None or not billing.remnawave_id:
        raise GracePanelError('Recovered canonical subscription has no Remnawave user id')

    target = _build_billing_target(billing, now=datetime.now(UTC))
    explicit_disabled = _billing_requires_explicit_disabled(billing)
    if target.status not in {PanelUserStatus.ACTIVE, PanelUserStatus.DISABLED}:
        raise GracePanelError(f'Canonical renewal unexpectedly resolved to derived panel status {target.status.value}')
    if target.status is PanelUserStatus.DISABLED:
        current_user = await api.get_user_by_id(billing.remnawave_id)
        if current_user is None:
            raise GracePanelError('Canonical Remnawave user disappeared during recovered DISABLED update')
        if not target.write_expire_at:
            target = replace(
                target,
                expire_at=_panel_user_to_snapshot(current_user).expire_at,
            )
        if explicit_disabled:
            updated = await _apply_canonical_disabled_target(
                api,
                remnawave_id=billing.remnawave_id,
                target=target,
                current_user=current_user,
                base_kwargs=update_kwargs,
            )
        else:
            updated = await _apply_restore_disabled_target(
                api,
                remnawave_id=billing.remnawave_id,
                target=target,
                current_user=current_user,
                base_kwargs=update_kwargs,
            )
        target_matches = updated is not None and (
            _panel_user_matches_disabled_target_exact(updated, target)
            if explicit_disabled
            else _panel_user_matches_restored_disabled_target(updated, target)
        )
    else:
        canonical_kwargs = _serialize_panel_target(
            billing.remnawave_id,
            target,
            base_kwargs=update_kwargs,
        )
        updated = await api.update_user(**canonical_kwargs)
        target_matches = updated is not None and _panel_user_matches_target(updated, target)
    if not target_matches:
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


async def apply_grace_tariff_switch_reset_locked(
    db: AsyncSession,
    subscription_id: int,
    *,
    source: str,
) -> bool:
    """Run the configured tariff reset while the caller holds the Grace lock."""
    if grace_access_runtime.mode not in {GraceAccessMode.ACTIVE, GraceAccessMode.DRAIN}:
        return False

    store = SQLAlchemyGraceSessionStore(db, subscription_id=subscription_id)
    if await store.get_open(subscription_id) is None:
        return False
    core = GraceAccessService(
        store=store,
        panel=RemnawaveGracePanelGateway(),
        billing=SQLAlchemyGraceBillingGateway(db),
        policy=_build_policy(),
    )
    action = await core.apply_tariff_switch_traffic_reset(subscription_id)
    if action is None:
        raise GracePanelTransitionConflict(
            'Open Grace session does not match the configured tariff-switch reset intent'
        )
    logger.info(
        'Grace-aware tariff-switch traffic reset completed',
        subscription_id=subscription_id,
        source=source,
        action=action,
    )
    return True


async def apply_grace_tariff_switch_reset(
    subscription_id: int,
    *,
    source: str,
) -> bool:
    """Acquire Grace locks and run the same reset for standalone admin syncs."""
    if grace_access_runtime.mode not in {GraceAccessMode.ACTIVE, GraceAccessMode.DRAIN}:
        return False
    async with grace_access_runtime._locks.hold(subscription_id):
        async with AsyncSessionLocal() as db:
            await lock_grace_sensitive_panel_updates(db, (subscription_id,))
            handled = await apply_grace_tariff_switch_reset_locked(
                db,
                subscription_id,
                source=source,
            )
            await db.commit()
            return handled


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

        # The kwarg the client identifies a panel user by is ``user_id`` since
        # Remnawave 3.0.0.  Both sides go through the same coercion so a numeric
        # string from FSM/JSON compares equal to the BigInteger column, and an
        # unusable value stays falsy instead of accidentally matching.
        supplied_id = _optional_panel_user_id(update_kwargs.get('user_id'))
        fresh_subscription = lease.subscription
        expected_id = _optional_panel_user_id(
            fresh_subscription.remnawave_id
            if settings.is_multi_tariff_enabled()
            else (fresh_subscription.user.remnawave_id if fresh_subscription.user else None)
        )
        if expected_id and supplied_id != expected_id:
            raise GracePanelError(f'Remnawave user id changed before subscription {subscription_id} update')

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

        current = await api.get_user_by_id(supplied_id)
        if current is None:
            raise GracePanelError(f'Remnawave user {supplied_id} disappeared while grace was open')
        return current


def _create_payload_as_patch(create_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Превратить payload создания в безопасный payload обновления.

    Две ловушки, из-за которых нельзя просто переслать create-тело в PATCH:

    * ``username`` — в 3.0.0 это альтернативный идентификатор записи, а команда
      требует ровно один; вместе с ``id`` он лишний.
    * ``active_internal_squads`` — ``create_user`` пропускает пустой список
      (``if active_internal_squads:``), а ``update_user`` — только ``None``
      (``if ... is not None``). В контракте поле опционально: не прислать =
      «не трогать», прислать ``[]`` = «снять все сквады». Переслав пустой
      список, мы бы сняли у живого оплаченного аккаунта все инбаунды — он
      остался бы ACTIVE, но ссылка на подписку отдавала бы ноль конфигов.
      Ровно поэтому все остальные update-ветки в проекте гейтят это поле
      через ``if subscription.connected_squads:``.
    """
    patch = {key: value for key, value in create_kwargs.items() if key != 'username'}
    if not patch.get('active_internal_squads'):
        patch.pop('active_internal_squads', None)
    return patch


async def _adopt_or_create(api: Any, adopt_short_uuid: str | None, create_kwargs: dict[str, Any]) -> Any:
    """Опознать существующего панельного пользователя по shortUuid, иначе создать.

    У строки, привязанной до апгрейда на Remnawave 3.0.0, числового id нет (его
    проставляет бэкфил), но shortUuid панель по-прежнему знает. Без этой проверки
    любое админское «создать/синхронизировать» заводит ВТОРОЙ панельный аккаунт,
    затирает shortUuid новым — и оплаченный оригинал становится ненаходимым.

    Проверка живёт здесь, потому что через этот хелпер проходят все админские
    пути создания; дублировать её по call-site значит однажды забыть.
    """
    short_uuid = (adopt_short_uuid or '').strip()
    if short_uuid:
        # Только 404 (→ None) доказывает, что аккаунта нет. Любая другая ошибка
        # пробрасывается: создать нового «на всякий случай» — это и есть дубль.
        adopted = await api.get_user_by_short_uuid(short_uuid)
        if adopted is not None:
            # Подхватить аккаунт мало — вызывающий просил ПРИВЕСТИ панель к
            # переданному состоянию и трактует результат как «панель теперь
            # такая». Без PATCH админское «продлить» отрапортовало бы успех,
            # оставив в панели старые статус/дату/лимиты, а у клиента —
            # нерабочий VPN. `username` в PATCH не идёт: это create-only поле,
            # переименовывать существующий аккаунт мы не собираемся.
            update_kwargs = _create_payload_as_patch(create_kwargs)
            return await api.update_user(user_id=adopted.id, **update_kwargs)
    return await api.create_user(**create_kwargs)


async def create_panel_user_grace_safe(
    api: Any,
    subscription_id: int,
    *,
    adopt_short_uuid: str | None = None,
    **create_kwargs: Any,
) -> Any:
    """Create a panel user only while the subscription cannot have an overlay."""
    if grace_access_runtime.mode in (GraceAccessMode.DISABLED, GraceAccessMode.OBSERVE):
        # Non-mutating grace: оверлеев не существует/не защищаются — создаём напрямую.
        return await _adopt_or_create(api, adopt_short_uuid, create_kwargs)
    async with grace_sensitive_panel_update(subscription_id) as lease:
        if lease.subscription is None:
            raise GracePanelError(f'Subscription {subscription_id} disappeared before Remnawave user creation')
        if lease.has_open_grace:
            raise GracePanelError(
                f'Remnawave user creation deferred while subscription {subscription_id} has open grace'
            )
        return await _adopt_or_create(api, adopt_short_uuid, create_kwargs)


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
    remnawave_id: int,
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

    Идентификатор приводится к числу СРАЗУ: ниже он уходит не только в панель,
    но и в ``WHERE remnawave_id = :value``. Непригодное значение там дало бы
    ``IS NULL`` — то есть совпадение со ВСЕМИ неслинкованными подписками.
    """
    panel_user_id = coerce_panel_user_id(remnawave_id)
    if grace_access_runtime.mode in (GraceAccessMode.DISABLED, GraceAccessMode.OBSERVE):
        # Non-mutating grace: не трогаем ни БД, ни suppression-маркеры —
        # поведение панельного enable/disable как до фичи. Остаточные открытые
        # сессии в этих режимах уже отрапортованы CRITICAL-логом на старте.
        if enabled:
            return await api.enable_user(panel_user_id)
        return await api.disable_user(panel_user_id)

    if db is not None:
        action_result, deferred_disable_error = await _set_panel_user_enabled_state_locked(
            db, api, panel_user_id, enabled=enabled
        )
        if deferred_disable_error is not None:
            raise deferred_disable_error
        return action_result

    async with AsyncSessionLocal() as guard_db:
        async with guard_db.begin():
            action_result, deferred_disable_error = await _set_panel_user_enabled_state_locked(
                guard_db, api, panel_user_id, enabled=enabled
            )
    if deferred_disable_error is not None:
        raise deferred_disable_error
    return action_result


async def _set_panel_user_enabled_state_locked(
    guard_db: AsyncSession,
    api: Any,
    remnawave_id: int,
    *,
    enabled: bool,
) -> tuple[Any, BaseException | None]:
    action_result: Any = None
    deferred_disable_error: BaseException | None = None
    identity_mapping_filter = (
        Subscription.remnawave_id == remnawave_id
        if settings.is_multi_tariff_enabled()
        else User.remnawave_id == remnawave_id
    )
    mapped_ids = {
        int(value)
        for value in (
            await guard_db.execute(
                select(Subscription.id).join(User, Subscription.user_id == User.id).where(identity_mapping_filter)
            )
        ).scalars()
    }
    open_subscription_ids = {
        int(value)
        for value in (
            await guard_db.execute(
                select(GraceAccessSessionModel.subscription_id).where(
                    GraceAccessSessionModel.remnawave_id == remnawave_id,
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
            action_result = await api.enable_user(remnawave_id)
        else:
            action_result = await api.disable_user(remnawave_id)
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
            current = await api.get_user_by_id(remnawave_id)
            if current is None:
                state_error = GracePanelError(f'Remnawave user {remnawave_id} disappeared during status update')
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
        traffic_bytes=settings.GRACE_ACCESS_TRAFFIC_GB * gib,
        trial_enabled=settings.GRACE_ACCESS_TRIAL_ENABLED,
        daily_enabled=settings.GRACE_ACCESS_DAILY_ENABLED,
        free_enabled=settings.GRACE_ACCESS_FREE_ENABLED,
        reconcile_batch_size=settings.GRACE_ACCESS_RECONCILE_BATCH_SIZE,
        reset_traffic_on_tariff_switch=settings.RESET_TRAFFIC_ON_TARIFF_SWITCH,
    )


def _validate_active_configuration() -> None:
    if settings.GRACE_ACCESS_TRAFFIC_GB < 1:
        raise ValueError('GRACE_ACCESS_TRAFFIC_GB must be at least 1 when GRACE_ACCESS_MODE=true')
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
    from app.services.subscription_service import get_traffic_reset_strategy

    remnawave_id = subscription.remnawave_id if settings.is_multi_tariff_enabled() else user.remnawave_id
    traffic_limit_gb = max(0, int(subscription.traffic_limit_gb or 0))
    traffic_used_gb = max(0.0, float(subscription.traffic_used_gb or 0.0))
    return GraceBillingState(
        subscription_id=subscription.id,
        remnawave_id=remnawave_id,
        status=subscription.actual_status,
        end_at=_as_utc(subscription.end_date) if subscription.end_date else None,
        traffic_limit_bytes=traffic_limit_gb * 1024**3,
        used_traffic_bytes=int(traffic_used_gb * 1024**3),
        device_limit=subscription.device_limit,
        squad_uuids=_string_tuple(subscription.connected_squads),
        traffic_limit_strategy=get_traffic_reset_strategy(tariff).value,
        external_squad_uuid=(tariff.external_squad_uuid if tariff else None),
        is_trial=bool(subscription.is_trial or subscription.status == SubscriptionStatus.TRIAL.value),
        is_daily=bool(tariff and tariff.is_daily),
        is_free_tariff=bool(tariff and tariff.is_free),
        user_status=user.status,
        grace_suppressed_until=(
            _as_utc(subscription.grace_suppressed_until) if subscription.grace_suppressed_until else None
        ),
        tariff_id=subscription.tariff_id,
        tariff_id_known=True,
    )


def _panel_user_to_snapshot(panel_user: Any) -> GracePanelSnapshot:
    return GracePanelSnapshot(
        # coerce вместо str(): панель 3.0.0 обязана вернуть числовой id, а
        # прежний str(panel_user.uuid) на None молча записывал строку 'None'.
        remnawave_id=coerce_panel_user_id(panel_user.id),
        status=_normalize(panel_user.status),
        expire_at=_as_utc(panel_user.expire_at) if panel_user.expire_at else None,
        traffic_limit_bytes=int(panel_user.traffic_limit_bytes or 0),
        used_traffic_bytes=int(panel_user.used_traffic_bytes or 0),
        squad_uuids=_extract_panel_squads(panel_user.active_internal_squads),
        external_squad_uuid=panel_user.external_squad_uuid,
        traffic_is_known=panel_user.user_traffic is not None,
        last_traffic_reset_at=(_as_utc(panel_user.last_traffic_reset_at) if panel_user.last_traffic_reset_at else None),
        traffic_limit_strategy=_strategy_value(getattr(panel_user, 'traffic_limit_strategy', None)),
    )


def _build_restore_target(snapshot: GracePanelSnapshot, *, now: datetime) -> _PanelTarget:
    status = _normalize(snapshot.status)
    expire_at = _as_utc(snapshot.expire_at) if snapshot.expire_at else now
    safe_expire_after = _as_utc(now) + _GRACE_EXPIRE_AT_SAFETY_MARGIN
    if status == 'expired':
        return _PanelTarget(
            status=PanelUserStatus.EXPIRED,
            expire_at=expire_at,
            traffic_limit_bytes=snapshot.traffic_limit_bytes,
            squad_uuids=snapshot.squad_uuids,
            external_squad_uuid=snapshot.external_squad_uuid,
            traffic_limit_strategy=snapshot.traffic_limit_strategy,
            write_expire_at=False,
        )
    if status == 'disabled' or expire_at <= now:
        return _PanelTarget(
            status=PanelUserStatus.DISABLED,
            expire_at=expire_at,
            traffic_limit_bytes=snapshot.traffic_limit_bytes,
            squad_uuids=snapshot.squad_uuids,
            external_squad_uuid=snapshot.external_squad_uuid,
            traffic_limit_strategy=snapshot.traffic_limit_strategy,
            write_expire_at=status == 'disabled' and expire_at > safe_expire_after,
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
        traffic_limit_strategy=snapshot.traffic_limit_strategy,
    )


def _build_billing_target(billing: GraceBillingState, *, now: datetime) -> _PanelTarget:
    status = _normalize(billing.status)
    user_active = _normalize(billing.user_status) == DatabaseUserStatus.ACTIVE.value
    expire_at = _as_utc(billing.end_at) if billing.end_at else now
    safe_expire_after = _as_utc(now) + _GRACE_EXPIRE_AT_SAFETY_MARGIN
    if user_active and status in {'active', 'trial'} and expire_at > now:
        panel_status = PanelUserStatus.ACTIVE
        write_expire_at = True
    elif user_active and status == 'limited' and expire_at > now:
        panel_status = PanelUserStatus.LIMITED
        write_expire_at = True
    else:
        panel_status = PanelUserStatus.DISABLED
        write_expire_at = expire_at > safe_expire_after
    return _PanelTarget(
        status=panel_status,
        expire_at=expire_at,
        traffic_limit_bytes=billing.traffic_limit_bytes,
        squad_uuids=billing.squad_uuids,
        external_squad_uuid=billing.external_squad_uuid,
        traffic_limit_strategy=billing.traffic_limit_strategy,
        device_limit=billing.device_limit,
        write_expire_at=write_expire_at,
    )


def _billing_requires_explicit_disabled(billing: GraceBillingState) -> bool:
    return (
        _normalize(billing.status) == SubscriptionStatus.DISABLED.value
        or _normalize(billing.user_status) != DatabaseUserStatus.ACTIVE.value
    )


def _serialize_panel_target(
    remnawave_id: int,
    target: _PanelTarget,
    *,
    base_kwargs: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a writable Remnawave payload without sending derived statuses."""
    kwargs = _serialize_grace_panel_patch(
        remnawave_id,
        status=target.status,
        expire_at=target.expire_at,
        write_expire_at=target.write_expire_at,
        base_kwargs={
            **dict(base_kwargs or {}),
            'traffic_limit_bytes': target.traffic_limit_bytes,
            'active_internal_squads': list(target.squad_uuids),
            'external_squad_uuid': target.external_squad_uuid,
        },
        now=now,
    )
    if target.device_limit is not None:
        kwargs['hwid_device_limit'] = target.device_limit
    if target.write_traffic_limit_strategy and target.traffic_limit_strategy is not None:
        kwargs['traffic_limit_strategy'] = TrafficLimitStrategy(target.traffic_limit_strategy)
    return kwargs


async def _restore_target_in_phases(
    api: Any,
    *,
    remnawave_id: int,
    target: _PanelTarget,
    current_user: Any,
) -> Any:
    """Restore canonical fields while keeping reset automation detached.

    Strategy and external squad are isolated PATCHes.  A lost response can
    therefore be confirmed independently and cannot combine a routing change
    with the final strategy restoration in one ambiguous request.
    """
    current = _panel_user_to_snapshot(current_user)
    if current.traffic_limit_strategy != TrafficLimitStrategy.NO_RESET.value:
        current_user = await api.update_user(
            user_id=remnawave_id,
            traffic_limit_strategy=TrafficLimitStrategy.NO_RESET,
        )
        if current_user is None:
            current_user = await api.get_user_by_id(remnawave_id)
        if current_user is None:
            return None
        current = _panel_user_to_snapshot(current_user)
        if current.traffic_limit_strategy not in {TrafficLimitStrategy.NO_RESET.value, None}:
            raise GracePanelError('Remnawave did not confirm NO_RESET during restore')

    canonical_target = replace(
        target,
        external_squad_uuid=current.external_squad_uuid,
        traffic_limit_strategy=TrafficLimitStrategy.NO_RESET.value,
        write_traffic_limit_strategy=False,
    )
    if not _panel_matches_target(current, canonical_target):
        current_user = await api.update_user(**_serialize_panel_target(remnawave_id, canonical_target))
        if current_user is None:
            current_user = await api.get_user_by_id(remnawave_id)
        if current_user is None:
            return None
        current = _panel_user_to_snapshot(current_user)
        if not _panel_matches_target(current, canonical_target):
            raise GracePanelError('Remnawave did not confirm canonical restore fields')

    if current.external_squad_uuid != target.external_squad_uuid:
        current_user = await api.update_user(
            user_id=remnawave_id,
            external_squad_uuid=target.external_squad_uuid,
        )
        if current_user is None:
            current_user = await api.get_user_by_id(remnawave_id)
        if current_user is None:
            return None
        current = _panel_user_to_snapshot(current_user)
        if current.external_squad_uuid != target.external_squad_uuid:
            raise GracePanelError('Remnawave did not confirm external squad restoration')

    if target.traffic_limit_strategy is not None:
        current_user = await api.update_user(
            user_id=remnawave_id,
            traffic_limit_strategy=TrafficLimitStrategy(target.traffic_limit_strategy),
        )
        if current_user is None:
            current_user = await api.get_user_by_id(remnawave_id)
        if current_user is None:
            return None
        current = _panel_user_to_snapshot(current_user)
        if current.traffic_limit_strategy != target.traffic_limit_strategy:
            raise GracePanelError('Remnawave did not confirm canonical traffic reset strategy')
    return current_user


def _serialize_grace_panel_patch(
    remnawave_id: int,
    *,
    status: PanelUserStatus,
    expire_at: datetime | None = None,
    write_expire_at: bool = False,
    base_kwargs: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a Grace-owned PATCH and enforce Remnawave writable invariants.

    Remnawave rejects an elapsed ``expireAt``. A deadline that is only barely
    in the future is equally unsafe because it can elapse in the request queue
    or because the bot and panel clocks differ. ACTIVE/LIMITED transitions are
    therefore aborted before any PATCH is sent; a fail-closed DISABLED update
    may proceed without rewriting the unsafe deadline.
    """
    normalized_status = _normalize(status)
    writable_status: PanelUserStatus | None
    if normalized_status == 'active':
        writable_status = PanelUserStatus.ACTIVE
    elif normalized_status == 'disabled':
        writable_status = PanelUserStatus.DISABLED
    elif normalized_status in {'limited', 'expired'}:
        writable_status = None
    else:
        raise GracePanelError(f'Unsupported Grace panel status {status!r}')

    kwargs = dict(base_kwargs or {})
    # A caller-provided value must never bypass the Grace status/date policy.
    kwargs.pop('status', None)
    kwargs.pop('expire_at', None)
    kwargs['user_id'] = remnawave_id

    if writable_status is not None:
        kwargs['status'] = writable_status

    if write_expire_at and normalized_status != 'expired':
        if expire_at is None:
            raise GracePanelError('Writable Grace panel target is missing expire_at')
        if expire_at.tzinfo is None or expire_at.utcoffset() is None:
            raise GracePanelError('Grace expire_at must be timezone-aware')
        reference_now = now or datetime.now(UTC)
        if reference_now.tzinfo is None or reference_now.utcoffset() is None:
            raise GracePanelError('Grace expiration reference time must be timezone-aware')
        expire_at_utc = expire_at.astimezone(UTC)
        safe_after = reference_now.astimezone(UTC) + _GRACE_EXPIRE_AT_SAFETY_MARGIN
        if expire_at_utc <= safe_after:
            if normalized_status != 'disabled':
                raise GracePanelError('Grace expire_at is not safely in the future; Remnawave PATCH was not sent')
        else:
            kwargs['expire_at'] = expire_at_utc

    return kwargs


def _panel_matches_limited_intermediate(
    snapshot: GracePanelSnapshot,
    target: _PanelTarget,
    expected_overlay: GracePanelOverlay,
    *,
    statuses: frozenset[str] = frozenset({'active', 'limited'}),
) -> bool:
    return (
        _normalize(snapshot.status) in statuses
        and snapshot.expire_at is not None
        and abs((_as_utc(snapshot.expire_at) - _as_utc(target.expire_at)).total_seconds()) <= 2
        and snapshot.traffic_limit_bytes == target.traffic_limit_bytes
        and set(snapshot.squad_uuids) == set(expected_overlay.squad_uuids)
        and snapshot.external_squad_uuid == expected_overlay.external_squad_uuid
        and _strategy_matches(snapshot.traffic_limit_strategy, expected_overlay.traffic_limit_strategy)
    )


def _limited_transition_source_is_safe(
    current: GracePanelSnapshot,
    target: _PanelTarget,
    expected_overlay: GracePanelOverlay,
    *,
    now: datetime,
) -> bool:
    if _panel_matches_limited_intermediate(current, target, expected_overlay):
        return True

    current_status = _normalize(current.status)
    overlay_status_is_safe = current_status in {'active', 'limited'} or (
        current_status == 'expired' and _as_utc(now) >= _as_utc(expected_overlay.expire_at)
    )
    return overlay_status_is_safe and panel_matches_overlay(
        current,
        expected_overlay,
        now=now,
    )


def _limited_source_matches_previous_restore(
    current: GracePanelSnapshot,
    previous_snapshot: GracePanelSnapshot,
    expected_overlay: GracePanelOverlay,
    *,
    now: datetime,
) -> bool:
    """Accept only an exact state produced by the immediately preceding restore."""
    if not _reset_generations_equal(
        current.last_traffic_reset_at,
        previous_snapshot.last_traffic_reset_at,
    ):
        return False
    if current.used_traffic_bytes < previous_snapshot.used_traffic_bytes:
        return False
    previous_target = _build_restore_target(previous_snapshot, now=now)
    if previous_target.status is not PanelUserStatus.LIMITED:
        return False
    return _panel_matches_target(current, previous_target) or _panel_matches_limited_intermediate(
        current,
        previous_target,
        expected_overlay,
    )


async def _restore_expired_target(
    api: Any,
    *,
    remnawave_id: int,
    target: _PanelTarget,
    snapshot: GracePanelSnapshot,
    expected_overlay: GracePanelOverlay,
    current_user: Any,
    now: datetime,
    force_disable: bool,
) -> GraceRestoreOutcome:
    """Restore an EXPIRED snapshot without writing status or expiration time."""
    current = _panel_user_to_snapshot(current_user)
    preserved_target = replace(target, expire_at=current.expire_at)
    retained_expiry_is_known = _optional_datetimes_equal(current.expire_at, snapshot.expire_at) or (
        current.expire_at is not None
        and abs((_as_utc(current.expire_at) - _as_utc(expected_overlay.expire_at)).total_seconds()) <= 2
    )
    if retained_expiry_is_known and _panel_matches_target(current, preserved_target):
        return GraceRestoreOutcome.ALREADY_RESTORED

    current_status = _normalize(current.status)
    restored_disabled_target = replace(
        preserved_target,
        status=PanelUserStatus.DISABLED,
        write_expire_at=False,
    )
    if (
        retained_expiry_is_known
        and current_status == 'disabled'
        and _panel_matches_target(current, restored_disabled_target)
    ):
        # Idempotent recovery after an early-drain PATCH reached Remnawave but
        # the process stopped before the Grace completion commit.
        return GraceRestoreOutcome.ALREADY_RESTORED

    if _panel_matches_disabled_overlay_intermediate(current, expected_overlay):
        # The status-only PATCH is deliberately phase A. If phase B failed or the
        # process stopped, retry only the canonical field PATCH from this exact,
        # access-safe intermediate instead of treating it as a manual conflict.
        updated = await _apply_restore_disabled_target(
            api,
            remnawave_id=remnawave_id,
            target=restored_disabled_target,
            current_user=current_user,
        )
        return GraceRestoreOutcome.RESTORED if updated is not None else GraceRestoreOutcome.CONFLICT

    overlay_matches = panel_matches_overlay(
        current,
        expected_overlay,
        now=now,
    )
    if current_status in {'active', 'limited'}:
        if not overlay_matches:
            return GraceRestoreOutcome.CONFLICT
        if not force_disable and _as_utc(now) >= _as_utc(expected_overlay.expire_at):
            # Remnawave owns EXPIRED.  Keep the restricted Grace routing until
            # its watchdog derives the status; exposing canonical squads while
            # the user is still ACTIVE would briefly restore unrestricted access.
            raise GracePanelTransitionPending('Remnawave has not derived EXPIRED for the elapsed grace overlay')

        # Emergency/early drain cannot wait for a future overlay deadline.
        # Disable first, then restore fields without mixing node-removal and
        # squad changes in one Remnawave command. Preserve the panel expiration.
        updated = await _apply_restore_disabled_target(
            api,
            remnawave_id=remnawave_id,
            target=restored_disabled_target,
            current_user=current_user,
        )
        return GraceRestoreOutcome.RESTORED if updated is not None else GraceRestoreOutcome.CONFLICT

    if current_status != 'expired':
        # DISABLED is an external/manual revocation here, not the derived status
        # expected from a naturally elapsed Grace overlay.
        return GraceRestoreOutcome.CONFLICT
    if not overlay_matches and not panel_is_safe_pending_source(
        current,
        snapshot,
        expected_overlay,
    ):
        return GraceRestoreOutcome.CONFLICT

    updated = await api.update_user(**_serialize_panel_target(remnawave_id, preserved_target))
    if updated is not None and _panel_matches_target(_panel_user_to_snapshot(updated), preserved_target):
        return GraceRestoreOutcome.RESTORED
    verified_user = await api.get_user_by_id(remnawave_id)
    if verified_user is not None and _panel_matches_target(
        _panel_user_to_snapshot(verified_user),
        preserved_target,
    ):
        return GraceRestoreOutcome.RESTORED
    return GraceRestoreOutcome.CONFLICT


async def _apply_limited_target(
    api: Any,
    *,
    remnawave_id: int,
    target: _PanelTarget,
    expected_overlay: GracePanelOverlay,
    current_user: Any,
) -> Any | None:
    """Restore a derived LIMITED target without exposing canonical routing early."""
    intermediate = _panel_user_to_snapshot(current_user)
    if not _panel_matches_limited_intermediate(
        intermediate,
        target,
        expected_overlay,
    ) or not _panel_user_matches_device_limit(current_user, target):
        phase_a_kwargs: dict[str, Any] = _serialize_grace_panel_patch(
            remnawave_id,
            status=target.status,
            expire_at=target.expire_at,
            write_expire_at=True,
            base_kwargs={
                'traffic_limit_bytes': target.traffic_limit_bytes,
                'active_internal_squads': list(expected_overlay.squad_uuids),
                'external_squad_uuid': expected_overlay.external_squad_uuid,
            },
        )
        if target.device_limit is not None:
            phase_a_kwargs['hwid_device_limit'] = target.device_limit
        phase_a_user = await api.update_user(**phase_a_kwargs)
        if phase_a_user is None:
            phase_a_user = await api.get_user_by_id(remnawave_id)
        if phase_a_user is None:
            return None
        intermediate = _panel_user_to_snapshot(phase_a_user)
        if not _panel_user_matches_device_limit(phase_a_user, target):
            return None

    if _panel_matches_limited_intermediate(
        intermediate,
        target,
        expected_overlay,
        statuses=frozenset({'active'}),
    ):
        raise GracePanelTransitionPending('Remnawave has not derived LIMITED after applying canonical quota fields')
    if not _panel_matches_limited_intermediate(
        intermediate,
        target,
        expected_overlay,
        statuses=frozenset({'limited'}),
    ):
        return None

    phase_b_user = await api.update_user(
        user_id=remnawave_id,
        active_internal_squads=list(target.squad_uuids),
        external_squad_uuid=target.external_squad_uuid,
    )
    if phase_b_user is not None and _panel_user_matches_target(phase_b_user, target):
        return phase_b_user

    verified_user = await api.get_user_by_id(remnawave_id)
    if verified_user is not None and _panel_user_matches_target(verified_user, target):
        return verified_user
    return None


def _panel_matches_disabled_overlay_intermediate(
    snapshot: GracePanelSnapshot,
    expected_overlay: GracePanelOverlay,
) -> bool:
    return (
        _normalize(snapshot.status) == 'disabled'
        and _optional_datetimes_equal(snapshot.expire_at, expected_overlay.expire_at)
        and snapshot.traffic_limit_bytes == expected_overlay.traffic_limit_bytes
        and set(snapshot.squad_uuids) == set(expected_overlay.squad_uuids)
        and snapshot.external_squad_uuid == expected_overlay.external_squad_uuid
        and _strategy_matches(snapshot.traffic_limit_strategy, expected_overlay.traffic_limit_strategy)
    )


async def _apply_canonical_disabled_target(
    api: Any,
    *,
    remnawave_id: int,
    target: _PanelTarget,
    current_user: Any,
    base_kwargs: Mapping[str, Any] | None = None,
) -> Any | None:
    """Apply canonical DISABLED via the dedicated action, then patch fields."""
    if target.status is not PanelUserStatus.DISABLED:
        raise GracePanelError('Disabled target helper received a non-DISABLED status')

    disabled_user = current_user
    if _normalize(getattr(disabled_user, 'status', None)) != 'disabled':
        try:
            disabled_user = await api.disable_user(remnawave_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The action may have reached Remnawave even when its response was
            # lost, or a concurrent actor may have disabled the user first.
            disabled_user = await api.get_user_by_id(remnawave_id)
            if disabled_user is None or _normalize(getattr(disabled_user, 'status', None)) != 'disabled':
                raise

        if disabled_user is None or _normalize(getattr(disabled_user, 'status', None)) != 'disabled':
            disabled_user = await api.get_user_by_id(remnawave_id)

        if disabled_user is None or _normalize(getattr(disabled_user, 'status', None)) != 'disabled':
            return None

    updated = await _patch_disabled_target_fields(
        api,
        remnawave_id=remnawave_id,
        target=target,
        base_kwargs=base_kwargs,
    )
    if updated is not None and _panel_user_matches_disabled_target_exact(updated, target):
        return updated
    return None


async def _apply_restore_disabled_target(
    api: Any,
    *,
    remnawave_id: int,
    target: _PanelTarget,
    current_user: Any,
    base_kwargs: Mapping[str, Any] | None = None,
) -> Any | None:
    """Restore a fail-closed status without emitting a canonical user.disabled."""
    if target.status is not PanelUserStatus.DISABLED:
        raise GracePanelError('Restore-disabled helper received a non-DISABLED status')

    current_status = _normalize(getattr(current_user, 'status', None))
    if current_status == 'limited':
        # Generic PATCH cannot force LIMITED -> DISABLED in Remnawave 2.8.
        # Keep the already access-safe state and retry after the watchdog derives
        # EXPIRED rather than emit user.disabled and mutate canonical billing.
        raise GracePanelTransitionPending('Remnawave LIMITED cannot be safely disabled by a restore PATCH')
    if current_status == 'active':
        try:
            disabled_user = await api.update_user(
                **_serialize_grace_panel_patch(
                    remnawave_id,
                    status=PanelUserStatus.DISABLED,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            disabled_user = await api.get_user_by_id(remnawave_id)
            if disabled_user is None or _normalize(getattr(disabled_user, 'status', None)) != 'disabled':
                raise
        if disabled_user is None or _normalize(getattr(disabled_user, 'status', None)) != 'disabled':
            disabled_user = await api.get_user_by_id(remnawave_id)
        if disabled_user is None:
            return None
        current_status = _normalize(getattr(disabled_user, 'status', None))
        if current_status != 'disabled':
            if current_status in {'active', 'limited', 'expired'}:
                raise GracePanelTransitionPending('Remnawave did not confirm the restore DISABLED transition yet')
            return None
        current_user = disabled_user
    elif current_status not in {'disabled', 'expired'}:
        return None

    updated = await _patch_disabled_target_fields(
        api,
        remnawave_id=remnawave_id,
        target=target,
        base_kwargs=base_kwargs,
    )
    if updated is not None and _panel_user_matches_restored_disabled_target(updated, target):
        return updated
    return None


async def _patch_disabled_target_fields(
    api: Any,
    *,
    remnawave_id: int,
    target: _PanelTarget,
    base_kwargs: Mapping[str, Any] | None = None,
) -> Any | None:
    field_kwargs = _serialize_panel_target(
        remnawave_id,
        target,
        base_kwargs=base_kwargs,
    )
    field_kwargs.pop('status', None)
    updated = await api.update_user(**field_kwargs)
    if updated is None:
        updated = await api.get_user_by_id(remnawave_id)
    return updated


def _panel_user_matches_device_limit(panel_user: Any, target: _PanelTarget) -> bool:
    if target.device_limit is None:
        return True
    raw_limit = getattr(panel_user, 'hwid_device_limit', None)
    try:
        return raw_limit is not None and int(raw_limit) == target.device_limit
    except (TypeError, ValueError):
        return False


def _strategy_matches(actual: str | None, expected: str | None) -> bool:
    """Compare strategies when both sides expose the v4 field.

    ``None`` is the explicit legacy/partial-read marker.  It must not be
    guessed as a concrete strategy, but allowing it through here keeps v2/v3
    sessions reconcilable until the live panel is read and the snapshot is
    upgraded by the activation path.
    """
    return expected is None or actual is None or actual == expected


def _panel_user_matches_target(panel_user: Any, target: _PanelTarget) -> bool:
    return _panel_matches_target(
        _panel_user_to_snapshot(panel_user),
        target,
    ) and _panel_user_matches_device_limit(panel_user, target)


def _panel_user_matches_disabled_target_exact(panel_user: Any, target: _PanelTarget) -> bool:
    """Verify the explicit DISABLED state; derived EXPIRED is not equivalent here."""
    return _panel_user_matches_disabled_target_statuses(
        panel_user,
        target,
        statuses=frozenset({'disabled'}),
    )


def _panel_user_matches_restored_disabled_target(panel_user: Any, target: _PanelTarget) -> bool:
    return _panel_user_matches_disabled_target_statuses(
        panel_user,
        target,
        statuses=frozenset({'disabled', 'expired'}),
    )


def _panel_user_matches_disabled_target_statuses(
    panel_user: Any,
    target: _PanelTarget,
    *,
    statuses: frozenset[str],
) -> bool:
    snapshot = _panel_user_to_snapshot(panel_user)
    return (
        _normalize(snapshot.status) in statuses
        and _optional_datetimes_equal(snapshot.expire_at, target.expire_at)
        and snapshot.traffic_limit_bytes == target.traffic_limit_bytes
        and set(snapshot.squad_uuids) == set(target.squad_uuids)
        and snapshot.external_squad_uuid == target.external_squad_uuid
        and _strategy_matches(snapshot.traffic_limit_strategy, target.traffic_limit_strategy)
        and _panel_user_matches_device_limit(panel_user, target)
    )


def _panel_matches_target(snapshot: GracePanelSnapshot, target: _PanelTarget) -> bool:
    actual_status = _normalize(snapshot.status)
    expected_status = _normalize(target.status)
    if expected_status == 'disabled':
        status_matches = actual_status in {'disabled', 'expired'}
        expiry_matches = True
    elif expected_status == 'expired':
        status_matches = actual_status == 'expired'
        expiry_matches = _optional_datetimes_equal(snapshot.expire_at, target.expire_at)
    else:
        status_matches = actual_status == expected_status
        expiry_matches = bool(
            snapshot.expire_at
            and target.expire_at
            and abs((_as_utc(snapshot.expire_at) - _as_utc(target.expire_at)).total_seconds()) <= 2
        )
    return (
        status_matches
        and expiry_matches
        and snapshot.traffic_limit_bytes == target.traffic_limit_bytes
        and set(snapshot.squad_uuids) == set(target.squad_uuids)
        and snapshot.external_squad_uuid == target.external_squad_uuid
        and _strategy_matches(snapshot.traffic_limit_strategy, target.traffic_limit_strategy)
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
    # ``remnawave_uuid`` is deliberately absent: a new row cannot know a uuid the
    # panel no longer returns, and an UPDATE that omits the key keeps whatever
    # historical value a pre-3.0.0 row still carries for auditing.
    return {
        'subscription_id': session.subscription_id,
        'remnawave_id': session.remnawave_id,
        'reason': session.reason.value,
        'incident_key': session.incident_key,
        'state': session.state.value,
        'snapshot_version': _SNAPSHOT_VERSION,
        'billing_before': _billing_to_json(session.billing_before),
        'panel_before': _panel_to_json(session.panel_before),
        'overlay': _session_overlay_to_json(session),
        'started_at': _as_utc(session.started_at),
        'grace_until': _as_utc(session.grace_until),
        'updated_at': _as_utc(session.updated_at),
        'completion_reason': session.completion_reason.value if session.completion_reason else None,
        'completed_at': _as_utc(session.completed_at) if session.completed_at else None,
        'last_error': session.last_error,
    }


def _model_to_session(model: GraceAccessSessionModel) -> GraceAccessSession:
    if model.snapshot_version not in _SUPPORTED_SNAPSHOT_VERSIONS:
        supported = ', '.join(str(version) for version in sorted(_SUPPORTED_SNAPSHOT_VERSIONS))
        raise GraceSnapshotError(f'Unsupported grace snapshot version {model.snapshot_version}; supported: {supported}')
    # An empty column means the identity backfill never reached this row.  That
    # is a data fault — never "the panel user is gone" — so it surfaces as a
    # snapshot error with last_error instead of a silent restore-less close.
    remnawave_id = _panel_user_id(model.remnawave_id, 'grace_access_sessions.remnawave_id')
    return GraceAccessSession(
        id=model.id,
        subscription_id=model.subscription_id,
        remnawave_id=remnawave_id,
        reason=GraceReason(model.reason),
        incident_key=model.incident_key,
        state=GraceSessionState(model.state),
        billing_before=_billing_from_json(model.billing_before),
        panel_before=_panel_from_json(model.panel_before, fallback_remnawave_id=remnawave_id),
        overlay=_overlay_from_json(model.overlay),
        started_at=_as_utc(model.started_at),
        grace_until=_as_utc(model.grace_until),
        updated_at=_as_utc(model.updated_at),
        completion_reason=(GraceCompletionReason(model.completion_reason) if model.completion_reason else None),
        completed_at=_as_utc(model.completed_at) if model.completed_at else None,
        last_error=model.last_error,
        version=model.version,
        incident_aliases=_incident_aliases_from_overlay_json(model.overlay),
        limited_lineage_tail=_limited_lineage_tail_from_overlay_json(model.overlay),
        allow_recovery_enabled_webhook=_allow_recovery_enabled_from_overlay_json(model.overlay),
        traffic_reset_target=_traffic_reset_target_from_overlay_json(model.overlay),
        traffic_reset_remaining_bytes=_traffic_reset_remaining_from_overlay_json(model.overlay),
        traffic_reset_started_at=_traffic_reset_started_at_from_overlay_json(model.overlay),
        traffic_reset_finished_at=_traffic_reset_finished_at_from_overlay_json(model.overlay),
    )


def _panel_matches_inactive_overlay(
    snapshot: GracePanelSnapshot,
    expected_overlay: GracePanelOverlay,
) -> bool:
    """Match an exact reset-owned state whose status already denies access."""
    return (
        _normalize(snapshot.status) in {'disabled', 'expired'}
        and _optional_datetimes_equal(snapshot.expire_at, expected_overlay.expire_at)
        and snapshot.traffic_limit_bytes == expected_overlay.traffic_limit_bytes
        and set(snapshot.squad_uuids) == set(expected_overlay.squad_uuids)
        and snapshot.external_squad_uuid == expected_overlay.external_squad_uuid
    )


def _billing_to_json(value: GraceBillingState) -> dict[str, Any]:
    return {
        'subscription_id': value.subscription_id,
        'remnawave_id': value.remnawave_id,
        'status': value.status,
        'end_at': _datetime_to_json(value.end_at),
        'traffic_limit_bytes': value.traffic_limit_bytes,
        'used_traffic_bytes': value.used_traffic_bytes,
        'device_limit': value.device_limit,
        'squad_uuids': list(value.squad_uuids),
        'traffic_limit_strategy': value.traffic_limit_strategy,
        'external_squad_uuid': value.external_squad_uuid,
        'is_trial': value.is_trial,
        'is_daily': value.is_daily,
        'is_free_tariff': value.is_free_tariff,
        'user_status': value.user_status,
        'grace_suppressed_until': _datetime_to_json(value.grace_suppressed_until),
        'tariff_id': value.tariff_id,
        'tariff_id_known': value.tariff_id_known,
    }


def _billing_from_json(raw: Any) -> GraceBillingState:
    data = _mapping(raw, 'billing_before')
    return GraceBillingState(
        subscription_id=_integer(data, 'subscription_id'),
        # v2 blobs carry only the legacy uuid string here; it is unusable in
        # 3.0.0 and no decision reads this field, so ``None`` is correct.
        remnawave_id=_optional_panel_user_id(data.get('remnawave_id')),
        status=_string(data, 'status'),
        end_at=_datetime_from_json(data.get('end_at')),
        traffic_limit_bytes=_integer(data, 'traffic_limit_bytes'),
        used_traffic_bytes=_integer(data, 'used_traffic_bytes'),
        device_limit=_optional_integer(data.get('device_limit')),
        squad_uuids=_string_tuple(data.get('squad_uuids')),
        traffic_limit_strategy=_strategy_value(data.get('traffic_limit_strategy')),
        external_squad_uuid=_optional_string(data.get('external_squad_uuid')),
        is_trial=bool(data.get('is_trial', False)),
        is_daily=bool(data.get('is_daily', False)),
        is_free_tariff=bool(data.get('is_free_tariff', False)),
        user_status=str(data.get('user_status', 'active')),
        grace_suppressed_until=_datetime_from_json(data.get('grace_suppressed_until')),
        tariff_id=_optional_integer(data.get('tariff_id')),
        tariff_id_known=bool(data.get('tariff_id_known', False)),
    )


def _panel_to_json(value: GracePanelSnapshot) -> dict[str, Any]:
    return {
        'remnawave_id': value.remnawave_id,
        'status': value.status,
        'expire_at': _datetime_to_json(value.expire_at),
        'traffic_limit_bytes': value.traffic_limit_bytes,
        'used_traffic_bytes': value.used_traffic_bytes,
        'squad_uuids': list(value.squad_uuids),
        'traffic_limit_strategy': value.traffic_limit_strategy,
        'external_squad_uuid': value.external_squad_uuid,
        'traffic_is_known': value.traffic_is_known,
        'last_traffic_reset_at': _datetime_to_json(value.last_traffic_reset_at),
    }


def _panel_from_json(raw: Any, *, fallback_remnawave_id: int | None = None) -> GracePanelSnapshot:
    data = _mapping(raw, 'panel_before')
    return GracePanelSnapshot(
        # A v2 blob predates the numeric identity and only holds the legacy uuid
        # string.  Its session row was backfilled from the same subscription, so
        # the session column is the correct — and only — replacement.
        remnawave_id=_panel_user_id(
            data.get('remnawave_id') or fallback_remnawave_id,
            'panel_before.remnawave_id',
        ),
        status=_string(data, 'status'),
        expire_at=_datetime_from_json(data.get('expire_at')),
        traffic_limit_bytes=_integer(data, 'traffic_limit_bytes'),
        used_traffic_bytes=_integer(data, 'used_traffic_bytes'),
        squad_uuids=_string_tuple(data.get('squad_uuids')),
        external_squad_uuid=_optional_string(data.get('external_squad_uuid')),
        traffic_is_known=bool(data.get('traffic_is_known', True)),
        last_traffic_reset_at=_datetime_from_json(data.get('last_traffic_reset_at')),
        traffic_limit_strategy=_strategy_value(data.get('traffic_limit_strategy')),
    )


def _overlay_to_json(value: GracePanelOverlay) -> dict[str, Any]:
    return {
        'status': value.status,
        'expire_at': _datetime_to_json(value.expire_at),
        'traffic_limit_bytes': value.traffic_limit_bytes,
        'squad_uuids': list(value.squad_uuids),
        'traffic_limit_strategy': value.traffic_limit_strategy,
        'expected_last_traffic_reset_at': _datetime_to_json(value.expected_last_traffic_reset_at),
        'external_squad_uuid': value.external_squad_uuid,
    }


def _session_overlay_to_json(session: GraceAccessSession) -> dict[str, Any]:
    data = _overlay_to_json(session.overlay)
    if session.incident_aliases:
        data['_incident_aliases'] = list(session.incident_aliases)
    if session.limited_lineage_tail is not None:
        data['_limited_lineage_tail'] = _billing_to_json(session.limited_lineage_tail)
    if session.allow_recovery_enabled_webhook:
        data['_allow_recovery_enabled_webhook'] = True
    if session.traffic_reset_target is not None:
        data['_traffic_reset_target'] = _billing_to_json(session.traffic_reset_target)
        if session.traffic_reset_remaining_bytes is None:
            raise GraceSnapshotError('traffic reset target requires remaining byte count')
    if session.traffic_reset_remaining_bytes is not None:
        data['_traffic_reset_remaining_bytes'] = session.traffic_reset_remaining_bytes
    if session.traffic_reset_started_at is not None:
        data['_traffic_reset_started_at'] = _datetime_to_json(session.traffic_reset_started_at)
    if session.traffic_reset_finished_at is not None:
        data['_traffic_reset_finished_at'] = _datetime_to_json(session.traffic_reset_finished_at)
    return data


def _incident_aliases_from_overlay_json(raw: Any) -> tuple[str, ...]:
    data = _mapping(raw, 'overlay')
    return _string_tuple(data.get('_incident_aliases'))


def _limited_lineage_tail_from_overlay_json(raw: Any) -> GraceBillingState | None:
    data = _mapping(raw, 'overlay')
    tail = data.get('_limited_lineage_tail')
    return _billing_from_json(tail) if tail is not None else None


def _allow_recovery_enabled_from_overlay_json(raw: Any) -> bool:
    data = _mapping(raw, 'overlay')
    return bool(data.get('_allow_recovery_enabled_webhook', False))


def _traffic_reset_target_from_overlay_json(raw: Any) -> GraceBillingState | None:
    data = _mapping(raw, 'overlay')
    target = data.get('_traffic_reset_target')
    return _billing_from_json(target) if target is not None else None


def _traffic_reset_remaining_from_overlay_json(raw: Any) -> int | None:
    data = _mapping(raw, 'overlay')
    value = data.get('_traffic_reset_remaining_bytes')
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GraceSnapshotError('_traffic_reset_remaining_bytes must be a non-negative integer')
    return value


def _traffic_reset_started_at_from_overlay_json(raw: Any) -> datetime | None:
    data = _mapping(raw, 'overlay')
    return _datetime_from_json(data.get('_traffic_reset_started_at'))


def _traffic_reset_finished_at_from_overlay_json(raw: Any) -> datetime | None:
    data = _mapping(raw, 'overlay')
    return _datetime_from_json(data.get('_traffic_reset_finished_at'))


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
        traffic_limit_strategy=_strategy_value(data.get('traffic_limit_strategy')),
        expected_last_traffic_reset_at=_datetime_from_json(data.get('expected_last_traffic_reset_at')),
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


def _panel_user_id(value: Any, label: str) -> int:
    """Read a required numeric Remnawave user id from a snapshot or a column.

    Accepts the digit strings a one-shot backfill script may write into JSON.
    Everything else is a broken link in our own data, which is why it raises a
    snapshot error rather than degrading to "no panel user".
    """
    try:
        return coerce_panel_user_id(value)
    except RemnaWaveInvalidUserIdError as error:
        raise GraceSnapshotError(f'{label} must be a positive Remnawave user id, got {value!r}') from error


def _optional_panel_user_id(value: Any) -> int | None:
    """Same coercion for identifiers that are legitimately absent."""
    if value is None:
        return None
    try:
        return coerce_panel_user_id(value)
    except RemnaWaveInvalidUserIdError:
        return None


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


def _optional_datetimes_equal(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return left is right
    return abs((_as_utc(left) - _as_utc(right)).total_seconds()) <= 2


def _reset_generations_equal(left: datetime | None, right: datetime | None) -> bool:
    """Compare reset generations exactly so two rapid resets stay distinct."""
    if left is None or right is None:
        return left is right
    return _as_utc(left) == _as_utc(right)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _strategy_value(value: object) -> str | None:
    if value is None:
        return None
    raw = getattr(value, 'value', value)
    if not isinstance(raw, str):
        raise GraceSnapshotError(f'Invalid traffic limit strategy value: {value!r}')
    normalized = raw.strip().upper()
    if normalized not in _TRAFFIC_LIMIT_STRATEGIES:
        raise GraceSnapshotError(f'Unsupported traffic limit strategy: {raw!r}')
    return normalized


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
