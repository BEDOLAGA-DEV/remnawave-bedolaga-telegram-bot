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
from types import SimpleNamespace
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
    UserStatus as PanelUserStatus,
    coerce_panel_user_id,
)
from app.services.grace_access_codec import (
    GraceSnapshotError,
    _as_utc,
    _model_to_session,
    _optional_panel_user_id,
    _session_to_model,
    _session_values,
    _string_tuple,
)
from app.services.grace_access_notifications import announce_grace_event
from app.services.grace_access_service import (
    GraceAccessMode,
    GraceAccessPolicy,
    GraceAccessService,
    GraceAccessSession,
    GraceBillingState,
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
    billing_is_eligible,
    build_incident_key,
    panel_is_safe_pending_source,
    panel_matches_overlay,
)
from app.services.panel_sync import is_subscription_live, panel_date_is_closing, panel_expire_at
from app.services.panel_sync.payload import resolve_panel_status


logger = structlog.get_logger(__name__)

_OPEN_STATES = (
    GraceSessionState.PENDING.value,
    GraceSessionState.ACTIVE.value,
    GraceSessionState.RESTORING.value,
)
_POSTGRES_LOCK_NAMESPACE = 1_196_572_995
_POSTGRES_GLOBAL_PANEL_LOCK_ID = 0


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


async def _mark_subscription_grace_open(db: AsyncSession, subscription_id: int, *, open_: bool) -> None:
    """Признак открытого грейса на подписке — в той же транзакции, что и сессия.

    Импорт «панель — истина» (``project_onto_subscription``) и мониторинг читают
    его сами: пока открыт, оверлей грейса в панели не выдаётся за продление.
    Пишет только хранилище — единственный, кто меняет состояние сессии.
    """
    await db.execute(update(Subscription).where(Subscription.id == subscription_id).values(grace_session_open=open_))


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
        if model is None:
            return None
        if model.remnawave_id is None:
            await _repair_missing_panel_id(self._db, model)
        return _model_to_session(model)

    async def create(self, session: GraceAccessSession) -> GraceAccessSession:
        model = _session_to_model(session)
        try:
            async with self._db.begin_nested():
                self._db.add(model)
                await self._db.flush()
                await _mark_subscription_grace_open(self._db, session.subscription_id, open_=True)
                # Дата оверлея — до PATCH в панель и навсегда: снимок панели с ней —
                # оверлей, даже если его обработают после досрочного закрытия грейса.
                await self._db.execute(
                    update(Subscription)
                    .where(Subscription.id == session.subscription_id)
                    .values(grace_overlay_expire_at=_as_utc(session.overlay.expire_at))
                )
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

        await _mark_subscription_grace_open(
            self._db,
            session.subscription_id,
            open_=session.state is not GraceSessionState.COMPLETED,
        )
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
    #: ``None`` — дату в панели не менять. Так уходят отключённые подписки: их
    #: настоящую дату окончания затирать нельзя, а проверка совпадения для
    #: DISABLED дату и не сверяет (см. _panel_matches_target).
    expire_at: datetime | None
    traffic_limit_bytes: int
    squad_uuids: tuple[str, ...]
    external_squad_uuid: str | None
    device_limit: int | None = None


class RemnawaveGracePanelGateway:
    """Changes only fields controlled by the temporary overlay."""

    def __init__(self, *, db: AsyncSession | None = None, subscription_id: int | None = None) -> None:
        # Сессия и подписка нужны одному: записать хвост грейса — дату, которую
        # PATCH оставляет в панели, — в той же транзакции, что и состояние сессии.
        self._db = db
        self._subscription_id = subscription_id

    async def _remember_grace_tail(self, expire_at: datetime | None) -> None:
        """Запомнить на подписке дату, которую грейс оставляет в панели.

        Прошедшую дату PATCH не принимает, вернуть настоящую нельзя — в панели
        остаётся конец грейса (или погашенная «сейчас плюс пять минут»). Импорт
        «панель — истина», увидев ровно её, дату и статус подписки не трогает:
        иначе истёкшая подписка «истекала» заново, а воркер выдавал грейс снова.
        """
        if self._db is None or self._subscription_id is None or expire_at is None:
            return
        await self._db.execute(
            update(Subscription)
            .where(Subscription.id == self._subscription_id)
            .values(grace_tail_expire_at=_as_utc(expire_at))
        )

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

            updated = await api.update_user(
                user_id=remnawave_id,
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
        remnawave_id: int,
        snapshot: GracePanelSnapshot,
        expected_overlay: GracePanelOverlay,
    ) -> GraceRestoreOutcome:
        from app.services.remnawave_service import remnawave_service

        now = datetime.now(UTC)

        async with remnawave_service.get_api_client() as api:
            # Only an explicit 404 reaches this as None.  A malformed local
            # identifier raises instead, so a data fault can never be mistaken
            # for "the panel user is gone, nothing to restore".
            current_user = await api.get_user_by_id(remnawave_id)
            if current_user is None:
                # A deleted panel user has no access left to revoke.
                return GraceRestoreOutcome.ALREADY_RESTORED

            current = _panel_user_to_snapshot(current_user)
            # Цель строится по тому, что стоит в панели сейчас: истёкшему снимку
            # прошедшую дату не вернуть, и общее правило гашения смотрит на неё.
            target = _build_restore_target(snapshot, now=now, panel_current=current.expire_at)
            if _panel_matches_target(current, target):
                return GraceRestoreOutcome.ALREADY_RESTORED
            if target.status is PanelUserStatus.EXPIRED:
                return await self._restore_expired_target(
                    api,
                    remnawave_id=remnawave_id,
                    target=target,
                    snapshot=snapshot,
                    expected_overlay=expected_overlay,
                    current=current,
                    now=now,
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

            updated = await api.update_user(**_serialize_panel_target(remnawave_id, target))
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

    async def _restore_expired_target(
        self,
        api: Any,
        *,
        remnawave_id: int,
        target: _PanelTarget,
        snapshot: GracePanelSnapshot,
        expected_overlay: GracePanelOverlay,
        current: GracePanelSnapshot,
        now: datetime,
    ) -> GraceRestoreOutcome:
        """Вернуть истёкшему снимку лимит и сквады, не трогая статус.

        Общее правило panel_sync: истёкшей подписке статус в панель не шлём —
        EXPIRED панель выводит сама, а DISABLED значит «отключена админом» и
        именно так импортируется в бота (кабинет тогда отказывает в продлении).
        Пока панель держит аккаунт ACTIVE с уже прошедшей (или погашенной нами)
        датой, сессия остаётся RESTORING и доводится следующим проходом.
        """
        if _expired_transition_is_pending(current, target, now=now):
            raise GracePanelTransitionPending(
                'Remnawave has not expired the account yet; canonical fields are in place'
            )
        if not panel_matches_overlay(current, expected_overlay, now=now) and not panel_is_safe_pending_source(
            current,
            snapshot,
            expected_overlay,
        ):
            return GraceRestoreOutcome.CONFLICT

        await self._remember_grace_tail(target.expire_at or current.expire_at)
        updated = await api.update_user(**_serialize_panel_target(remnawave_id, target))
        if updated is None:
            updated = await api.get_user_by_id(remnawave_id)
        if updated is None:
            raise GracePanelError('Remnawave restore PATCH could not be verified')
        after = _panel_user_to_snapshot(updated)
        if _panel_matches_target(after, target):
            return GraceRestoreOutcome.RESTORED
        if _expired_transition_is_pending(after, target, now=now):
            raise GracePanelTransitionPending(
                'Remnawave has not expired the account yet; canonical fields are in place'
            )
        return GraceRestoreOutcome.CONFLICT

    async def apply_billing_state(
        self,
        billing: GraceBillingState,
        *,
        expected_overlay: GracePanelOverlay,
    ) -> None:
        from app.services.remnawave_service import remnawave_service

        if not billing.remnawave_id:
            raise GracePanelError('Canonical subscription has no Remnawave user id')
        now = datetime.now(UTC)
        target = _build_billing_target(billing, now=now)

        async with remnawave_service.get_api_client() as api:
            if target.status is PanelUserStatus.EXPIRED:
                await self._apply_expired_billing_target(
                    api,
                    billing=billing,
                    expected_overlay=expected_overlay,
                    now=now,
                )
                return
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
                if not _limited_transition_source_is_safe(
                    current,
                    target,
                    expected_overlay,
                    now=now,
                ):
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
            else:
                updated = await api.update_user(**_serialize_panel_target(billing.remnawave_id, target))
        if updated is None or not _panel_user_matches_target(updated, target):
            if target.status is PanelUserStatus.LIMITED:
                raise GracePanelTransitionConflict('Remnawave changed while canonical LIMITED state was being applied')
            raise GracePanelError('Remnawave did not confirm canonical billing state')

    async def _apply_expired_billing_target(
        self,
        api: Any,
        *,
        billing: GraceBillingState,
        expected_overlay: GracePanelOverlay,
        now: datetime,
    ) -> None:
        """Канон истёкшей подписки: поля тарифа без статуса, EXPIRED выводит панель.

        Так закрываются конфликты посреди грейса (тариф, сквады или лимит
        поменяли, пока подписка истёкшая). Дата грейса в панели ещё в будущем —
        её гасит общее правило «ближайший допустимый момент», а до EXPIRED от
        планировщика сессия ждёт как RESTORING.
        """
        current_user = await api.get_user_by_id(billing.remnawave_id)
        if current_user is None:
            raise GracePanelTransitionConflict('Canonical Remnawave user disappeared during EXPIRED restore')
        current = _panel_user_to_snapshot(current_user)
        target = _build_billing_target(billing, now=now, panel_current=current.expire_at)
        if _panel_matches_target(current, target) and _panel_user_matches_device_limit(current_user, target):
            return
        if _expired_transition_is_pending(current, target, now=now) and _panel_user_matches_device_limit(
            current_user, target
        ):
            raise GracePanelTransitionPending(
                'Remnawave has not expired the account yet; canonical fields are in place'
            )
        if not _expired_transition_source_is_safe(current, target, expected_overlay, now=now):
            raise GracePanelTransitionConflict(
                'Remnawave changed outside grace; canonical EXPIRED state was not applied'
            )

        await self._remember_grace_tail(target.expire_at or current.expire_at)
        updated = await api.update_user(**_serialize_panel_target(billing.remnawave_id, target))
        if updated is None:
            updated = await api.get_user_by_id(billing.remnawave_id)
        if updated is None:
            raise GracePanelTransitionConflict('Canonical Remnawave user disappeared during EXPIRED restore')
        if _panel_user_matches_target(updated, target):
            return
        if _expired_transition_is_pending(
            _panel_user_to_snapshot(updated), target, now=now
        ) and _panel_user_matches_device_limit(updated, target):
            raise GracePanelTransitionPending(
                'Remnawave has not expired the account yet; canonical fields are in place'
            )
        raise GracePanelTransitionConflict('Remnawave changed while canonical EXPIRED state was being applied')


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
        # Бот для уведомлений о выдаче/завершении; ставит main.py, как у мониторинга.
        # Без него grace работает молча (CLI, тесты).
        self.bot: Any = None

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
            await self._announce_start(subscription_id, result)
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

    async def _announce_start(self, subscription_id: int, result: GraceStartResult) -> None:
        """Сообщить о свежей выдаче — уже после коммита, чтобы не объявлять то, что откатилось."""
        if result.decision in {GraceStartDecision.STARTED, GraceStartDecision.RETRIED}:
            await announce_grace_event(self.bot, subscription_id, 'granted')
        elif (
            result.decision is GraceStartDecision.SUPERSEDED
            and result.session is not None
            and result.session.state is GraceSessionState.COMPLETED
        ):
            await announce_grace_event(self.bot, subscription_id, 'ended')

    async def _announce_reconcile(self, subscription_id: int, result: GraceReconcileResult) -> None:
        if result.activated:
            await announce_grace_event(self.bot, subscription_id, 'granted')
        elif result.paid or result.timed_out or result.drained or result.revoked or result.conflicts:
            await announce_grace_event(self.bot, subscription_id, 'ended')

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
        # Уже после коммита и вне блокировки: уведомление не должно ни задерживать
        # согласователь, ни объявлять состояние, которое не записалось.
        await self._announce_reconcile(subscription_id, result)
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
    if target.status not in {PanelUserStatus.ACTIVE, PanelUserStatus.DISABLED}:
        raise GracePanelError(f'Canonical renewal unexpectedly resolved to derived panel status {target.status.value}')
    canonical_kwargs = _serialize_panel_target(
        billing.remnawave_id,
        target,
        base_kwargs=update_kwargs,
    )

    updated = await api.update_user(**canonical_kwargs)
    if updated is None or not _panel_user_matches_target(updated, target):
        raise GracePanelError('Remnawave did not confirm canonical billing state after renewal')

    completed = await core.complete_after_payment(
        subscription_id,
        apply_billing_state=False,
    )
    if not completed:
        raise GracePanelError('Recovered grace session changed before it could be completed')

    # Состояние сессии закоммитит вызывающий (продление, обычный апдейт панели):
    # объявлять раньше нельзя — уведомление читает сессию из базы и увидело бы
    # ещё открытую, а откат транзакции сделал бы объявление ложным.
    announce_grace_event_after_commit(db, subscription_id, 'ended')
    logger.info(
        'Grace access completed by the canonical renewal update',
        subscription_id=subscription_id,
        source=source,
    )
    return True, updated


#: Фоновые задачи уведомлений: без ссылки asyncio может собрать задачу до конца.
_announce_tasks: set[asyncio.Task[None]] = set()


def announce_grace_event_after_commit(db: AsyncSession, subscription_id: int, event: str) -> None:
    """Объявить о событии grace, когда вызывающий закоммитит свою транзакцию.

    Пути продления (CRUD, сервис подписок, обычный апдейт панели) закрывают grace
    внутри чужой транзакции. Хук ``after_commit`` срабатывает один раз и только
    на успешном коммите: откат — и объявления нет. Само уведомление — фоновая
    задача: оно не должно ни задерживать продление, ни уронить его сбоем.
    """
    from sqlalchemy import event as sa_event

    def _fire(_session: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(announce_grace_event(grace_access_runtime.bot, subscription_id, event))
        _announce_tasks.add(task)
        task.add_done_callback(_announce_tasks.discard)

    sa_event.listen(db.sync_session, 'after_commit', _fire, once=True)


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
    """Обычный панельный апдейт, не затирающий открытый grace; продление закрывает grace.

    О закрытии grace продлением объявляет ``apply_recovered_grace_update_locked``
    хуком после коммита вызывающего — одинаково для всех путей продления.
    """
    updated, _completed = await _update_panel_user_grace_safe_locked(api, subscription_id, **update_kwargs)
    return updated


async def _update_panel_user_grace_safe_locked(
    api: Any,
    subscription_id: int,
    **update_kwargs: Any,
) -> tuple[Any, bool]:
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
        return await api.update_user(**update_kwargs), False
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
            return await api.update_user(**update_kwargs), False

        completed, updated = await apply_recovered_grace_update_locked(
            lease.db,
            api,
            subscription_id,
            update_kwargs=update_kwargs,
            source='grace_safe_panel_update',
        )
        if completed:
            return updated, True

        protected_present = _GRACE_OWNED_UPDATE_FIELDS.intersection(update_kwargs)
        if not protected_present:
            return await api.update_user(**update_kwargs), False
        safe_kwargs = {key: value for key, value in update_kwargs.items() if key not in _GRACE_OWNED_UPDATE_FIELDS}
        logger.info(
            'Deferred grace-owned fields from routine Remnawave update',
            subscription_id=subscription_id,
            fields=sorted(protected_present),
        )
        if len(safe_kwargs) > 1:
            return await api.update_user(**safe_kwargs), False

        current = await api.get_user_by_id(supplied_id)
        if current is None:
            raise GracePanelError(f'Remnawave user {supplied_id} disappeared while grace was open')
        return current, False


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
        panel=RemnawaveGracePanelGateway(db=db, subscription_id=subscription_id),
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
        external_squad_uuid=settings.GRACE_ACCESS_EXTERNAL_SQUAD_UUID.strip() or None,
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


async def collect_grace_status(db: AsyncSession, *, error_limit: int = 20) -> dict[str, Any]:
    """Session counters and the newest failures, as one read-only snapshot.

    The emergency CLI and the cabinet page both report grace health, and a second
    copy of these queries would let the two drift: the rollback runbook compares
    the numbers an operator reads on screen with the ones the CLI prints before
    and after ``restore-all``. The returned mapping is that CLI payload, so its
    keys are a contract — the runbook quotes them.
    """
    state_rows = (
        await db.execute(
            select(GraceAccessSessionModel.state, func.count())
            .group_by(GraceAccessSessionModel.state)
            .order_by(GraceAccessSessionModel.state)
        )
    ).all()
    open_error_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(GraceAccessSessionModel)
                .where(
                    GraceAccessSessionModel.state.in_(_OPEN_STATES),
                    GraceAccessSessionModel.last_error.isnot(None),
                )
            )
        ).scalar_one()
    )
    completed_error_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(GraceAccessSessionModel)
                .where(
                    GraceAccessSessionModel.state == GraceSessionState.COMPLETED.value,
                    GraceAccessSessionModel.last_error.isnot(None),
                )
            )
        ).scalar_one()
    )
    error_rows = (
        await db.execute(
            select(
                GraceAccessSessionModel.id,
                GraceAccessSessionModel.subscription_id,
                GraceAccessSessionModel.state,
                GraceAccessSessionModel.completion_reason,
                GraceAccessSessionModel.last_error,
            )
            .where(GraceAccessSessionModel.last_error.isnot(None))
            .order_by(GraceAccessSessionModel.updated_at.desc())
            .limit(error_limit)
        )
    ).all()

    states = {str(state): int(count) for state, count in state_rows}
    open_count = sum(states.get(state, 0) for state in _OPEN_STATES)
    recent_errors = [
        {
            'id': str(session_id),
            'subscription_id': int(subscription_id),
            'state': str(state),
            'completion_reason': str(completion_reason) if completion_reason else None,
            'last_error': str(last_error),
        }
        for session_id, subscription_id, state, completion_reason, last_error in error_rows
    ]
    return {
        'open': open_count,
        'open_errors': open_error_count,
        'completed_errors': completed_error_count,
        'with_errors': open_error_count + completed_error_count,
        'states': states,
        'recent_errors': recent_errors,
    }


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
    )


def _build_restore_target(
    snapshot: GracePanelSnapshot,
    *,
    now: datetime,
    panel_current: datetime | None = None,
) -> _PanelTarget:
    """Каким аккаунт должен стать после отката грейса. ``panel_current`` — дата в панели сейчас."""
    status = _normalize(snapshot.status)
    expire_at = _as_utc(snapshot.expire_at) if snapshot.expire_at else now
    # Общее правило panel_sync: прошедшую дату при обновлении не шлём, а если в
    # панели стоит будущее (оверлей ещё не истёк — аварийный откат), гасим её
    # ближайшим допустимым моментом.
    closing_expire_at = panel_expire_at(
        expire_at, is_active=False, creating=False, now=now, panel_current=panel_current
    )
    if status == 'disabled':
        # Настоящее отключение админом — единственный случай, когда DISABLED наш.
        return _PanelTarget(
            status=PanelUserStatus.DISABLED,
            expire_at=closing_expire_at,
            traffic_limit_bytes=snapshot.traffic_limit_bytes,
            squad_uuids=snapshot.squad_uuids,
            external_squad_uuid=snapshot.external_squad_uuid,
        )
    if status == 'expired' or expire_at <= now:
        # EXPIRED панель выводит сама; DISABLED значил бы «отключена админом» —
        # именно так его импортирует бот, и кабинет отказывает в продлении.
        return _PanelTarget(
            status=PanelUserStatus.EXPIRED,
            expire_at=closing_expire_at,
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


def _build_billing_target(
    billing: GraceBillingState,
    *,
    now: datetime,
    panel_current: datetime | None = None,
) -> _PanelTarget:
    """Каким аккаунт должен стать по биллингу бота. ``panel_current`` — дата в панели сейчас."""
    status = _normalize(billing.status)
    expire_at = _as_utc(billing.end_at) if billing.end_at else now
    # Статус — общее правило синхронизации (panel_sync.payload): живая — ACTIVE,
    # заблокированный владелец или выключенная в боте — DISABLED, исчерпанный
    # трафик — LIMITED, истёкшая — EXPIRED. Два последних панель выводит сама,
    # и при обновлении они не отправляются (см. _serialize_panel_target).
    billing_user = SimpleNamespace(status=_normalize(billing.user_status))
    billing_subscription = SimpleNamespace(status=status, end_date=expire_at)
    is_live = is_subscription_live(billing_user, billing_subscription, now=now)
    panel_status = resolve_panel_status(billing_user, billing_subscription, is_live=is_live, now=now)
    # Дату считает общее правило: живой — её настоящую, истёкшей при обновлении
    # поле не отправляется вовсе (а будущую дату оверлея — гасит), чтобы не
    # затирать настоящий срок в панели.
    safe_expire_at = panel_expire_at(
        expire_at,
        is_active=panel_status is PanelUserStatus.ACTIVE,
        creating=False,
        now=now,
        panel_current=panel_current,
    )
    return _PanelTarget(
        status=panel_status,
        expire_at=safe_expire_at,
        traffic_limit_bytes=billing.traffic_limit_bytes,
        squad_uuids=billing.squad_uuids,
        external_squad_uuid=billing.external_squad_uuid,
        device_limit=billing.device_limit,
    )


def _serialize_panel_target(
    remnawave_id: int,
    target: _PanelTarget,
    *,
    base_kwargs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a writable Remnawave payload without sending derived statuses."""
    kwargs = dict(base_kwargs or {})
    kwargs.pop('status', None)
    kwargs.update(
        user_id=remnawave_id,
        traffic_limit_bytes=target.traffic_limit_bytes,
        active_internal_squads=list(target.squad_uuids),
        external_squad_uuid=target.external_squad_uuid,
    )
    if target.status in {PanelUserStatus.ACTIVE, PanelUserStatus.DISABLED}:
        kwargs['status'] = target.status
    elif target.status not in {PanelUserStatus.LIMITED, PanelUserStatus.EXPIRED}:
        raise GracePanelError(f'Unsupported canonical panel status {target.status!r}')
    if target.expire_at is not None:
        kwargs['expire_at'] = target.expire_at
    else:
        # Явно снимаем дату из базового набора: он собран для другого перехода,
        # и оставленная там дата затёрла бы настоящую.
        kwargs.pop('expire_at', None)
    if target.device_limit is not None:
        kwargs['hwid_device_limit'] = target.device_limit
    return kwargs


def _panel_matches_limited_intermediate(
    snapshot: GracePanelSnapshot,
    target: _PanelTarget,
    expected_overlay: GracePanelOverlay,
    *,
    statuses: frozenset[str] = frozenset({'active', 'limited'}),
) -> bool:
    # Промежуточное состояние строится только для LIMITED, а у него дата есть
    # всегда: без даты сверять нечего и совпадением это считать нельзя.
    return (
        _normalize(snapshot.status) in statuses
        and snapshot.expire_at is not None
        and target.expire_at is not None
        and abs((_as_utc(snapshot.expire_at) - _as_utc(target.expire_at)).total_seconds()) <= 2
        and snapshot.traffic_limit_bytes == target.traffic_limit_bytes
        and set(snapshot.squad_uuids) == set(expected_overlay.squad_uuids)
        and snapshot.external_squad_uuid == expected_overlay.external_squad_uuid
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


def _expired_transition_is_pending(
    snapshot: GracePanelSnapshot,
    target: _PanelTarget,
    *,
    now: datetime,
) -> bool:
    """Поля уже канонические, но панель ещё держит ACTIVE с прошедшей (или погашенной нами) датой."""
    return (
        _normalize(snapshot.status) == 'active'
        and snapshot.expire_at is not None
        and panel_date_is_closing(snapshot.expire_at, now=now)
        and snapshot.traffic_limit_bytes == target.traffic_limit_bytes
        and set(snapshot.squad_uuids) == set(target.squad_uuids)
        and snapshot.external_squad_uuid == target.external_squad_uuid
    )


def _expired_transition_source_is_safe(
    current: GracePanelSnapshot,
    target: _PanelTarget,
    expected_overlay: GracePanelOverlay,
    *,
    now: datetime,
) -> bool:
    return _expired_transition_is_pending(current, target, now=now) or panel_matches_overlay(
        current,
        expected_overlay,
        now=now,
    )


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
        phase_a_kwargs: dict[str, Any] = {
            'user_id': remnawave_id,
            'expire_at': target.expire_at,
            'traffic_limit_bytes': target.traffic_limit_bytes,
            'active_internal_squads': list(expected_overlay.squad_uuids),
            'external_squad_uuid': expected_overlay.external_squad_uuid,
        }
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


def _panel_user_matches_device_limit(panel_user: Any, target: _PanelTarget) -> bool:
    if target.device_limit is None:
        return True
    raw_limit = getattr(panel_user, 'hwid_device_limit', None)
    try:
        return raw_limit is not None and int(raw_limit) == target.device_limit
    except (TypeError, ValueError):
        return False


def _panel_user_matches_target(panel_user: Any, target: _PanelTarget) -> bool:
    return _panel_matches_target(
        _panel_user_to_snapshot(panel_user),
        target,
    ) and _panel_user_matches_device_limit(panel_user, target)


def _panel_matches_target(snapshot: GracePanelSnapshot, target: _PanelTarget) -> bool:
    actual_status = _normalize(snapshot.status)
    expected_status = _normalize(target.status)
    if expected_status == 'disabled':
        status_matches = actual_status in {'disabled', 'expired'}
        expiry_matches = True
    elif expected_status == 'expired':
        # Дату истёкшему аккаунту не вернуть — сверяем только статус и поля.
        # DISABLED сюда не входит: это чужое решение, не наш EXPIRED.
        status_matches = actual_status == 'expired'
        expiry_matches = True
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
