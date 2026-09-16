"""Грейс-сессии в базе: запись и чтение снимков, починка пустого номера в панели.

Вынесено из ``grace_access_runtime``, чтобы читать историю грейса могли и те, кого
импортирует сам рантайм через клиент панели, — продление подписки
(``grace_access_echo``) и цена продления. Иначе импорт шёл по кругу:
рантайм → сервис Remnawave → CRUD подписок → починка эха → рантайм.
Модуль тянет только модели, чистое ядро грейса и ошибки клиента панели — ни
настроек, ни клиента (через настройки импорт тоже замыкался на CRUD).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import GraceAccessSessionModel
from app.external.remnawave_errors import RemnaWaveInvalidUserIdError, coerce_panel_user_id
from app.services.grace_access_service import (
    GraceAccessSession,
    GraceBillingState,
    GraceCompletionReason,
    GracePanelOverlay,
    GracePanelSnapshot,
    GraceReason,
    GraceSessionState,
)


logger = structlog.get_logger(__name__)

_SNAPSHOT_VERSION = 3
# Version 3 stores the numeric Remnawave 3.0.0 identity.  Version 2 rows are
# still read: the backfill adds the numeric key *next to* the historical uuid
# instead of replacing it, and a session that predates the panel upgrade must
# stay reconcilable.  Refusing v2 here would make `_model_to_session` raise for
# every such row, `list_open` would drop them from the batch, and their overlay
# would never be rolled back — a permanently open door with no error report.
_SUPPORTED_SNAPSHOT_VERSIONS = frozenset({2, _SNAPSHOT_VERSION})


class GraceSnapshotError(ValueError):
    """A persisted snapshot is missing data required for a safe restore."""


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
        'overlay': _overlay_to_json(session.overlay),
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
        # v2 blobs carry only the legacy uuid string here; it is unusable in
        # 3.0.0 and no decision reads this field, so ``None`` is correct.
        remnawave_id=_optional_panel_user_id(data.get('remnawave_id')),
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
        'remnawave_id': value.remnawave_id,
        'status': value.status,
        'expire_at': _datetime_to_json(value.expire_at),
        'traffic_limit_bytes': value.traffic_limit_bytes,
        'used_traffic_bytes': value.used_traffic_bytes,
        'squad_uuids': list(value.squad_uuids),
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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def list_sessions_for_subscription(db: AsyncSession, subscription_id: int) -> list[GraceAccessSession]:
    """Все сессии подписки, открытые и закрытые, — её история грейсов.

    Нечитаемую строку (снимок старой версии без данных, сессия без номера в панели)
    пропускаем: история нужна, чтобы вернуть затёртое оверлеем v4.10–4.11, а те
    сессии записаны уже с номером; одна битая сессия не должна ронять продление.
    Номер здесь не дозаполняем — это запись, её делает согласователь грейса.
    """
    result = await db.execute(
        select(GraceAccessSessionModel).where(GraceAccessSessionModel.subscription_id == subscription_id)
    )
    sessions: list[GraceAccessSession] = []
    for model in result.scalars().all():
        try:
            sessions.append(_model_to_session(model))
        except ValueError as error:  # GraceSnapshotError — тоже ValueError
            logger.warning(
                'Грейс-сессия нечитаема — в истории подписки её не учитываем',
                grace_session_id=model.id,
                subscription_id=subscription_id,
                error=str(error)[:200],
            )
    return sessions
