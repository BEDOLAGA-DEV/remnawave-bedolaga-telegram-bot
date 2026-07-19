"""Mobile support WebSocket v1 endpoint for cabinet support tickets."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import mimetypes
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from aiogram.types import BufferedInputFile
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.websockets import WebSocketState

from app.bot_factory import create_bot
from app.cabinet.auth.jwt_handler import get_token_payload
from app.cabinet.auth.telegram_auth import validate_telegram_init_data
from app.cabinet.routes.media import (
    _BLOCKED_UPLOAD_CONTENT_TYPES,
    _BLOCKED_UPLOAD_EXTENSIONS,
    ALLOWED_MEDIA_TYPES,
    MAX_FILE_SIZE,
    _content_response_params,
    _resolve_target_chat_id,
)
from app.cabinet.routes.websocket import notify_admins_ticket_reply, notify_user_ticket_reply
from app.config import settings
from app.database.crud.rbac import SUPERADMIN_LEVEL, UserRoleCRUD
from app.database.crud.ticket_notification import TicketNotificationCRUD
from app.database.crud.user import get_user_by_id
from app.database.database import AsyncSessionLocal
from app.database.models import Ticket, TicketMessage, User, UserStatus
from app.services.blacklist_service import blacklist_service
from app.services.maintenance_service import maintenance_service
from app.services.permission_service import PermissionService
from app.services.rbac_bootstrap_service import is_user_admin_by_env
from app.services.user_revival_service import NotDeletedError, revive_deleted_user


logger = structlog.get_logger(__name__)

router = APIRouter()

SUPPORTED_SUBPROTOCOL = 'bedolaga.support.mobile.v1'
ERROR_CODES = {
    'AUTH_REQUIRED',
    'AUTH_EXPIRED',
    'FORBIDDEN',
    'NOT_FOUND',
    'VALIDATION_ERROR',
    'CONFLICT',
    'RATE_LIMITED',
    'BACKPRESSURE',
    'PAYLOAD_TOO_LARGE',
    'UNSUPPORTED_MEDIA_TYPE',
    'UPLOAD_NOT_FOUND',
    'UPLOAD_EXPIRED',
    'UPLOAD_CANCELLED',
    'UPLOAD_CHECKSUM_MISMATCH',
    'DOWNLOAD_NOT_FOUND',
    'DOWNLOAD_EXPIRED',
    'TICKET_CLOSED',
    'IDEMPOTENCY_CONFLICT',
    'INTERNAL_ERROR',
}
SUPPORT_ROLES = {'support', 'support_agent', 'support-manager', 'support_manager', 'moderator'}
ADMIN_ROLES = {'admin', 'administrator', 'superadmin', 'owner'}
MAX_UPLOAD_CHUNK_SIZE = 512 * 1024
DEFAULT_DOWNLOAD_CHUNK_SIZE = 256 * 1024
TRANSFER_TTL_SECONDS = 15 * 60
AUTH_EXPIRING_NOTICE_SECONDS = 5 * 60
# One base64 chunk (512 KiB -> ~683 KiB) plus envelope; reject larger frames before
# json.loads so a single client cannot force multi-hundred-MB parses into memory.
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
# Per-session ceilings so one connection cannot pin unbounded memory: in-flight
# transfers each buffer up to MAX_FILE_SIZE, and the completed/idempotency maps would
# otherwise grow for the life of the socket.
MAX_ACTIVE_TRANSFERS = 8
MAX_COMPLETED_MEDIA = 32
MAX_IDEMPOTENCY_KEYS = 256
ALLOWED_PRIORITIES = {'low', 'normal', 'high', 'urgent'}
ALLOWED_STATUSES = {'open', 'pending', 'answered', 'closed'}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None = None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def _now_iso() -> str:
    return _iso(_utc_now()) or ''


def _server_cursor(*parts: object) -> str:
    seed = ':'.join(str(part) for part in parts if part is not None)
    if not seed:
        seed = _now_iso()
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


def _snapshot_version(ticket: Ticket) -> str:
    return _server_cursor(ticket.id, _iso(ticket.updated_at), ticket.status, ticket.priority)


def _parse_int(value: Any, field_name: str, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{field_name} must be an integer') from exc
    if parsed < minimum:
        raise ValueError(f'{field_name} must be >= {minimum}')
    if maximum is not None and parsed > maximum:
        raise ValueError(f'{field_name} must be <= {maximum}')
    return parsed


def _parse_ticket_id(value: Any) -> int:
    return _parse_int(value, 'ticketId')


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value in (None, ''):
        return None
    if not isinstance(value, str):
        raise ValueError('updatedAfter must be an RFC3339 UTC string')
    try:
        normalized = value.replace('Z', '+00:00')
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError('updatedAfter must be an RFC3339 UTC string') from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _safe_list(value: Any, field_name: str) -> list[str]:
    if value in (None, ''):
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f'{field_name} must be an array of strings')
    return value


def _shared_error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    resource_type: str | None = None,
    resource_id: str | None = None,
    details: dict[str, Any] | None = None,
    retry_after_ms: int | None = None,
    backpressure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if code not in ERROR_CODES:
        code = 'INTERNAL_ERROR'
    return {
        'code': code,
        'message': message,
        'retryable': retryable,
        'resourceType': resource_type,
        'resourceId': resource_id,
        'details': details or {},
        'retryAfterMs': retry_after_ms,
        'backpressure': backpressure if code == 'BACKPRESSURE' else None,
    }


def _command_result(
    command: str,
    request_id: str,
    *,
    payload: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ok = error is None
    result: dict[str, Any] = {
        'type': 'command.result',
        'command': command,
        'requestId': request_id,
        'ok': ok,
        'receivedAt': _now_iso(),
    }
    if ok:
        result['payload'] = payload or {}
    else:
        result['error'] = error
    return result


def _message_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    ticket: Ticket | None = None,
) -> dict[str, Any]:
    ticket_id = str(ticket.id) if ticket is not None else payload.get('ticketId')
    return {
        'type': 'event',
        'event': event_type,
        'eventId': secrets.token_urlsafe(16),
        'serverCursor': _server_cursor(event_type, ticket_id, _now_iso(), secrets.token_hex(4)),
        'occurredAt': _now_iso(),
        'payload': payload,
    }


def _subprotocols(websocket: WebSocket) -> list[str]:
    raw = websocket.headers.get('sec-websocket-protocol', '')
    return [value.strip() for value in raw.split(',') if value.strip()]


def _is_blocked_upload(filename: str | None, content_type: str | None) -> bool:
    declared_type = (content_type or '').split(';', maxsplit=1)[0].strip().lower()
    filename_lower = (filename or '').lower()
    return declared_type in _BLOCKED_UPLOAD_CONTENT_TYPES or filename_lower.endswith(_BLOCKED_UPLOAD_EXTENSIONS)


def _guess_media_type(filename: str | None, content_type: str | None, requested: str | None) -> str:
    normalized = (requested or '').strip().lower()
    if normalized in ALLOWED_MEDIA_TYPES:
        return normalized
    mime = (content_type or mimetypes.guess_type(filename or '')[0] or '').lower()
    if mime.startswith('image/'):
        return 'photo'
    if mime.startswith('video/'):
        return 'video'
    return 'document'


@dataclass
class WsUserContext:
    user: User
    token_payload: dict[str, Any]
    role: str
    permissions: list[str] = field(default_factory=list)
    role_names: list[str] = field(default_factory=list)
    role_level: int = 0

    @property
    def user_id(self) -> int:
        return int(self.user.id)

    @property
    def exp_timestamp(self) -> int | None:
        exp = self.token_payload.get('exp')
        if isinstance(exp, int):
            return exp
        if isinstance(exp, float):
            return int(exp)
        return None


@dataclass
class UploadTransfer:
    upload_id: str
    owner_user_id: int
    ticket_id: int | None
    media_type: str
    file_name: str
    content_type: str | None
    size_bytes: int
    created_at: datetime
    chunks: bytearray = field(default_factory=bytearray)
    cancelled: bool = False

    @property
    def expired(self) -> bool:
        return (_utc_now() - self.created_at).total_seconds() > TRANSFER_TTL_SECONDS


@dataclass
class DownloadTransfer:
    download_id: str
    owner_user_id: int
    ticket_id: int
    media_id: str
    content: bytes
    file_name: str
    content_type: str
    headers: dict[str, str]
    created_at: datetime
    offset: int = 0
    cancelled: bool = False

    @property
    def expired(self) -> bool:
        return (_utc_now() - self.created_at).total_seconds() > TRANSFER_TTL_SECONDS


@dataclass(eq=False)
class SupportWsSession:
    websocket: WebSocket
    context: WsUserContext
    uploads: dict[str, UploadTransfer] = field(default_factory=dict)
    downloads: dict[str, DownloadTransfer] = field(default_factory=dict)
    completed_media: dict[str, dict[str, Any]] = field(default_factory=dict)
    idempotency: dict[str, str] = field(default_factory=dict)
    idempotency_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __hash__(self) -> int:
        return id(self)

    async def send_json(self, data: dict[str, Any]) -> None:
        # Broadcasts run from another user's task; serialize with the session's own
        # receive-loop sends so two coroutines never interleave frames on one socket.
        async with self.send_lock:
            await self.websocket.send_json(data)


class SupportWsManager:
    """Tracks active mobile support WebSocket sessions."""

    def __init__(self) -> None:
        self._sessions: set[SupportWsSession] = set()
        self._lock = asyncio.Lock()

    async def connect(self, session: SupportWsSession) -> None:
        async with self._lock:
            self._sessions.add(session)

    async def disconnect(self, session: SupportWsSession) -> None:
        async with self._lock:
            self._sessions.discard(session)

    async def broadcast_ticket_event(self, db: AsyncSession, ticket: Ticket, event: dict[str, Any]) -> None:
        async with self._lock:
            sessions = list(self._sessions)

        for session in sessions:
            try:
                if await _can_view_ticket(db, session.context, ticket):
                    await session.send_json(event)
            except Exception as exc:
                logger.warning('Support WS event delivery failed', user_id=session.context.user_id, error=str(exc))


support_ws_manager = SupportWsManager()


async def _role_context(db: AsyncSession, user: User, payload: dict[str, Any]) -> WsUserContext:
    permissions, role_names, role_level = await UserRoleCRUD.get_user_permissions(db, user.id)
    lower_roles = {role.lower() for role in role_names}
    is_legacy_admin = is_user_admin_by_env(user).is_admin

    if is_legacy_admin or role_level >= SUPERADMIN_LEVEL or ADMIN_ROLES.intersection(lower_roles):
        role = 'admin'
    elif SUPPORT_ROLES.intersection(lower_roles) or any(perm.startswith('tickets:') for perm in permissions):
        role = 'support'
    else:
        role = 'owner'

    return WsUserContext(
        user=user,
        token_payload=payload,
        role=role,
        permissions=permissions,
        role_names=role_names,
        role_level=role_level,
    )


def _user_status_value(user: User) -> str | None:
    status = getattr(user, 'status', None)
    return getattr(status, 'value', status)


async def _apply_cabinet_account_guards(
    db: AsyncSession,
    user: User,
    *,
    init_data_matches_user: bool,
) -> dict[str, Any] | None:
    if user.telegram_id is not None:
        is_blacklisted, _blacklist_reason = await blacklist_service.is_user_blacklisted(user.telegram_id, user.username)
        if is_blacklisted:
            return _shared_error('FORBIDDEN', 'User is blacklisted', resource_type='auth')

    status_value = _user_status_value(user)
    if status_value != UserStatus.ACTIVE.value:
        can_auto_revive = (
            status_value == UserStatus.DELETED.value and user.telegram_id is not None and init_data_matches_user
        )
        if can_auto_revive:
            try:
                await revive_deleted_user(db, user, source='cabinet_support_ws')
                await db.commit()
                await db.refresh(user)
            except NotDeletedError:
                logger.info('Support WS auto-revival race: user already revived', user_id=user.id)
        elif status_value == UserStatus.DELETED.value:
            return _shared_error(
                'FORBIDDEN', 'Account is deleted and must be restored through the bot', resource_type='auth'
            )
        else:
            return _shared_error('FORBIDDEN', 'User account is not active', resource_type='auth')

    if maintenance_service.is_maintenance_active() and not is_user_admin_by_env(user).is_admin:
        return _shared_error('FORBIDDEN', 'Service is under maintenance', resource_type='auth')

    if settings.CHANNEL_IS_REQUIRED_SUB and user.telegram_id is not None and not is_user_admin_by_env(user).is_admin:
        from app.services.channel_subscription_service import channel_subscription_service

        channels_with_status = await channel_subscription_service.get_channels_with_status(user.telegram_id)
        is_subscribed = (
            all(channel['is_subscribed'] for channel in channels_with_status) if channels_with_status else True
        )
        if not is_subscribed:
            return _shared_error('FORBIDDEN', 'Required channel subscription is missing', resource_type='auth')

    user.cabinet_last_login = _utc_now()
    try:
        await db.commit()
    except Exception:
        await db.rollback()

    return None


async def _authenticate_ws(
    db: AsyncSession,
    websocket: WebSocket,
    *,
    access_token: str | None = None,
) -> tuple[WsUserContext | None, dict[str, Any] | None]:
    auth_header = websocket.headers.get('authorization', '')
    token = access_token
    if token is None:
        scheme, _, credentials = auth_header.partition(' ')
        if scheme.lower() != 'bearer' or not credentials.strip():
            return None, _shared_error('AUTH_REQUIRED', 'Bearer access token is required', resource_type='auth')
        token = credentials.strip()

    payload = get_token_payload(token, expected_type='access')
    if not payload:
        return None, _shared_error('AUTH_EXPIRED', 'Access token is invalid or expired', resource_type='auth')

    try:
        user_id = int(payload.get('sub'))
    except (TypeError, ValueError):
        return None, _shared_error('AUTH_REQUIRED', 'Access token payload is invalid', resource_type='auth')

    user = await get_user_by_id(db, user_id)
    if not user:
        return None, _shared_error('AUTH_REQUIRED', 'User was not found', resource_type='auth')

    init_data_raw = websocket.headers.get('x-telegram-init-data')
    init_data_matches_user = False
    if init_data_raw and user.telegram_id is not None:
        tg_user = validate_telegram_init_data(init_data_raw, max_age_seconds=86400 * 30)
        if not tg_user or tg_user.get('id') != user.telegram_id:
            return None, _shared_error(
                'AUTH_REQUIRED',
                'Session belongs to a different Telegram account',
                resource_type='auth',
            )
        init_data_matches_user = True

    account_error = await _apply_cabinet_account_guards(db, user, init_data_matches_user=init_data_matches_user)
    if account_error is not None:
        return None, account_error

    return await _role_context(db, user, payload), None


async def _has_permission(db: AsyncSession, context: WsUserContext, permission: str) -> bool:
    allowed, _reason = await PermissionService.check_permission(db, context.user, permission)
    return allowed


def _ws_client_ip(websocket: WebSocket) -> str | None:
    forwarded_for = websocket.headers.get('x-forwarded-for') if hasattr(websocket, 'headers') else None
    if forwarded_for:
        return forwarded_for.split(',', maxsplit=1)[0].strip()
    client = getattr(websocket, 'client', None)
    return getattr(client, 'host', None)


async def _log_ws_permission_audit(
    db: AsyncSession,
    session: SupportWsSession,
    *,
    permission: str,
    command: str,
    status: str,
    ticket_id: int | None = None,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    resource_type = permission.split(':', maxsplit=1)[0] if ':' in permission else None
    audit_details: dict[str, Any] = {
        'method': 'WEBSOCKET',
        'path': '/cabinet/ws/support/v1',
        'command': command,
    }
    if ticket_id is not None:
        audit_details['ticketId'] = str(ticket_id)
    if reason:
        audit_details['reason'] = reason
    if details:
        audit_details.update(details)
    await PermissionService.log_action(
        db,
        user_id=session.context.user_id,
        action=permission,
        resource_type=resource_type,
        resource_id=str(ticket_id) if ticket_id is not None else None,
        details=audit_details,
        ip_address=_ws_client_ip(session.websocket),
        user_agent=session.websocket.headers.get('user-agent', '') if hasattr(session.websocket, 'headers') else '',
        status=status,
        request_method='WEBSOCKET',
        request_path='/cabinet/ws/support/v1',
    )


async def _require_ws_permission(
    db: AsyncSession,
    session: SupportWsSession,
    *,
    permission: str,
    command: str,
    ticket_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    allowed, reason = await PermissionService.check_permission(
        db,
        session.context.user,
        permission,
        ip_address=_ws_client_ip(session.websocket),
    )
    if not allowed:
        await _log_ws_permission_audit(
            db,
            session,
            permission=permission,
            command=command,
            status='denied',
            ticket_id=ticket_id,
            reason=reason,
        )
        await db.commit()
        raise PermissionError(f'Permission denied: {reason}')
    await _log_ws_permission_audit(
        db,
        session,
        permission=permission,
        command=command,
        status='success',
        ticket_id=ticket_id,
        details=details,
    )


async def _can_view_ticket(db: AsyncSession, context: WsUserContext, ticket: Ticket) -> bool:
    if context.role == 'owner':
        return ticket.user_id == context.user_id
    return await _has_permission(db, context, 'tickets:read')


async def _can_reply_ticket(db: AsyncSession, context: WsUserContext, ticket: Ticket) -> bool:
    if context.role == 'owner':
        return ticket.user_id == context.user_id
    return await _has_permission(db, context, 'tickets:reply')


async def _can_update_ticket(db: AsyncSession, context: WsUserContext) -> bool:
    if context.role == 'owner':
        return False
    return await _has_permission(db, context, 'tickets:close')


async def _get_visible_ticket(db: AsyncSession, context: WsUserContext, ticket_id: int) -> Ticket | None:
    query = (
        select(Ticket).where(Ticket.id == ticket_id).options(selectinload(Ticket.messages), selectinload(Ticket.user))
    )
    result = await db.execute(query)
    ticket = result.scalar_one_or_none()
    if ticket is None or not await _can_view_ticket(db, context, ticket):
        return None
    return ticket


def _user_snapshot(user: User | None) -> dict[str, Any] | None:
    if user is None:
        return None
    return {
        'id': str(user.id),
        'telegramId': str(user.telegram_id) if user.telegram_id is not None else None,
        'email': user.email,
        'username': user.username,
        'firstName': user.first_name,
        'lastName': user.last_name,
    }


def _media_items_from_message(message: TicketMessage) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    raw_items = getattr(message, 'media_items', None) or []
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict) or not item.get('file_id'):
                continue
            items.append(
                {
                    'mediaId': str(item.get('file_id')),
                    'type': item.get('type') or message.media_type or 'document',
                    'caption': item.get('caption'),
                    'download': {'transport': 'websocket', 'command': 'media.download.begin'},
                }
            )
    if message.media_file_id and not any(item['mediaId'] == str(message.media_file_id) for item in items):
        items.insert(
            0,
            {
                'mediaId': str(message.media_file_id),
                'type': message.media_type or 'document',
                'caption': message.media_caption,
                'download': {'transport': 'websocket', 'command': 'media.download.begin'},
            },
        )
    return items


def _message_snapshot(message: TicketMessage) -> dict[str, Any]:
    return {
        'id': str(message.id),
        'ticketId': str(message.ticket_id),
        'authorUserId': str(message.user_id),
        'isFromAdmin': bool(message.is_from_admin),
        'body': message.message_text or '',
        'attachments': _media_items_from_message(message),
        'createdAt': _iso(message.created_at),
    }


def _is_ticket_reply_blocked(ticket: Ticket) -> bool:
    return bool(getattr(ticket, 'is_user_reply_blocked', False) or getattr(ticket, 'is_reply_blocked', False))


def _ticket_snapshot(ticket: Ticket, *, include_messages: bool = False) -> dict[str, Any]:
    messages = sorted(ticket.messages or [], key=lambda item: item.created_at)
    snapshot: dict[str, Any] = {
        'id': str(ticket.id),
        'title': ticket.title or f'Ticket #{ticket.id}',
        'status': ticket.status,
        'priority': ticket.priority or 'normal',
        'createdAt': _iso(ticket.created_at),
        'updatedAt': _iso(ticket.updated_at or ticket.created_at),
        'closedAt': _iso(ticket.closed_at),
        'messagesCount': len(messages),
        'lastMessage': _message_snapshot(messages[-1]) if messages else None,
        'snapshotVersion': _snapshot_version(ticket),
        'assignedTo': None,
        'user': _user_snapshot(getattr(ticket, 'user', None)),
    }
    if include_messages:
        snapshot['isReplyBlocked'] = _is_ticket_reply_blocked(ticket)
        snapshot['messages'] = [_message_snapshot(message) for message in messages]
    return snapshot


async def _handle_ticket_list(db: AsyncSession, session: SupportWsSession, payload: dict[str, Any]) -> dict[str, Any]:
    context = session.context
    status_filters = _safe_list(payload.get('status'), 'status')
    priority_filters = _safe_list(payload.get('priority'), 'priority')
    if any(value not in ALLOWED_STATUSES for value in status_filters):
        raise ValueError('status contains unsupported value')
    if any(value not in ALLOWED_PRIORITIES for value in priority_filters):
        raise ValueError('priority contains unsupported value')

    limit = _parse_int(payload.get('limit', 20), 'limit', minimum=1, maximum=100)
    cursor = payload.get('cursor')
    offset = _parse_int(cursor, 'cursor', minimum=0) if cursor not in (None, '') else 0
    updated_after = _parse_iso_datetime(payload.get('updatedAfter'))
    query = select(Ticket).options(selectinload(Ticket.messages), selectinload(Ticket.user))
    count_query = select(func.count()).select_from(Ticket)

    if context.role == 'owner' or payload.get('mineOnly') is True:
        query = query.where(Ticket.user_id == context.user_id)
        count_query = count_query.where(Ticket.user_id == context.user_id)
    elif not await _has_permission(db, context, 'tickets:read'):
        raise PermissionError('tickets:read is required')

    if status_filters:
        query = query.where(Ticket.status.in_(status_filters))
        count_query = count_query.where(Ticket.status.in_(status_filters))
    if priority_filters:
        query = query.where(Ticket.priority.in_(priority_filters))
        count_query = count_query.where(Ticket.priority.in_(priority_filters))
    if updated_after is not None:
        query = query.where(Ticket.updated_at > updated_after)
        count_query = count_query.where(Ticket.updated_at > updated_after)

    assigned_to = payload.get('assignedTo')
    if assigned_to not in (None, '', 'null'):
        return {
            'tickets': [],
            'nextCursor': None,
            'serverCursor': _server_cursor('empty-assignment'),
            'asOf': _now_iso(),
        }

    result = await db.execute(query.order_by(desc(Ticket.updated_at)).offset(offset).limit(limit + 1))
    rows = result.scalars().all()
    page = rows[:limit]
    next_cursor = str(offset + limit) if len(rows) > limit else None
    as_of = _now_iso()
    return {
        'tickets': [_ticket_snapshot(ticket) for ticket in page],
        'nextCursor': next_cursor,
        'serverCursor': _server_cursor(*(ticket.id for ticket in page), as_of),
        'asOf': as_of,
    }


async def _handle_ticket_detail(db: AsyncSession, session: SupportWsSession, payload: dict[str, Any]) -> dict[str, Any]:
    ticket_id = _parse_ticket_id(payload.get('ticketId'))
    ticket = await _get_visible_ticket(db, session.context, ticket_id)
    if ticket is None:
        raise LookupError(str(ticket_id))

    message_limit = _parse_int(payload.get('messageLimit', 100), 'messageLimit', minimum=1, maximum=200)
    message_cursor = payload.get('messageCursor')
    offset = _parse_int(message_cursor, 'messageCursor', minimum=0) if message_cursor not in (None, '') else 0
    snapshot = _ticket_snapshot(ticket, include_messages=True)
    messages = snapshot.get('messages', [])
    snapshot['messages'] = messages[offset : offset + message_limit]
    snapshot['nextMessageCursor'] = str(offset + message_limit) if len(messages) > offset + message_limit else None
    return {
        'ticket': snapshot,
        'serverCursor': _server_cursor(ticket.id, ticket.updated_at, len(ticket.messages or [])),
        'asOf': _now_iso(),
    }


def _idempotency_fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _check_idempotency(session: SupportWsSession, key: Any, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Guard a command against duplicate execution under the same idempotencyKey.

    Returns the previously produced result for an identical retry (so the caller
    replays it without re-running side effects), raises IDEMPOTENCY_CONFLICT when
    the same key arrives with a different payload, and returns None for a first-seen
    key (recording its fingerprint so the eventual result can be cached).
    """
    if not key:
        return None
    if not isinstance(key, str):
        raise ValueError('idempotencyKey must be a string')
    fingerprint = _idempotency_fingerprint(payload)
    previous = session.idempotency.get(key)
    if previous and previous != fingerprint:
        raise RuntimeError('IDEMPOTENCY_CONFLICT')
    if previous == fingerprint:
        cached = session.idempotency_results.get(key)
        if cached is not None:
            return cached
    session.idempotency[key] = fingerprint
    while len(session.idempotency) > MAX_IDEMPOTENCY_KEYS:
        oldest = next(iter(session.idempotency))
        session.idempotency.pop(oldest, None)
        session.idempotency_results.pop(oldest, None)
    return None


def _store_idempotency_result(session: SupportWsSession, key: Any, result: dict[str, Any]) -> None:
    if isinstance(key, str) and key:
        session.idempotency_results[key] = result


async def _notify_ticket_reply_via_telegram(
    db: AsyncSession,
    ticket: Ticket,
    body: str,
    *,
    is_from_admin: bool,
    media_file_id: str | None = None,
    media_type: str | None = None,
) -> None:
    """Send the same Telegram notification the HTTP reply routes send.

    Admin/support reply -> DM the ticket owner (mirrors app/cabinet/routes/admin_tickets.py);
    user reply -> push to the admin bot chat (mirrors app/cabinet/routes/tickets.py).
    Best-effort: never let a notification failure break the reply flow.
    """
    try:
        if is_from_admin:
            from app.bot_factory import create_bot
            from app.handlers.admin.tickets import notify_user_about_ticket_reply

            bot = create_bot()
            try:
                await notify_user_about_ticket_reply(bot, ticket, body, db)
            finally:
                await bot.session.close()
        else:
            from app.handlers.tickets import notify_admins_about_ticket_reply

            await notify_admins_about_ticket_reply(ticket, body, db, media_file_id=media_file_id, media_type=media_type)
    except Exception as exc:
        logger.warning('Support WS Telegram reply notification failed', ticket_id=ticket.id, error=str(exc))


async def _handle_ticket_reply(db: AsyncSession, session: SupportWsSession, payload: dict[str, Any]) -> dict[str, Any]:
    ticket_id = _parse_ticket_id(payload.get('ticketId'))
    ticket = await _get_visible_ticket(db, session.context, ticket_id)
    if ticket is None:
        raise LookupError(str(ticket_id))
    is_from_admin = session.context.role in {'admin', 'support'}
    if not is_from_admin and not await _can_reply_ticket(db, session.context, ticket):
        raise PermissionError('tickets:reply is required')
    if ticket.status == 'closed':
        raise RuntimeError('TICKET_CLOSED')
    if session.context.role == 'owner' and _is_ticket_reply_blocked(ticket):
        raise PermissionError('Replies to this ticket are blocked')

    replay = _check_idempotency(session, payload.get('idempotencyKey'), payload)
    if replay is not None:
        return replay
    body = payload.get('body') or ''
    if not isinstance(body, str) or len(body) > 4000:
        raise ValueError('body must be a string up to 4000 characters')
    attachment_ids = payload.get('attachmentMediaIds') or []
    if not isinstance(attachment_ids, list) or not all(isinstance(item, str) for item in attachment_ids):
        raise ValueError('attachmentMediaIds must be an array of strings')
    if not body.strip() and not attachment_ids:
        raise ValueError('body or attachmentMediaIds is required')

    media_items = []
    for media_id in attachment_ids:
        item = session.completed_media.get(media_id)
        if item is None or item.get('ownerUserId') != session.context.user_id:
            raise LookupError(media_id)
        media_items.append({'type': item['type'], 'file_id': media_id, 'caption': item.get('caption')})

    primary = media_items[0] if media_items else None
    if is_from_admin:
        await _require_ws_permission(
            db,
            session,
            permission='tickets:reply',
            command='ticket.reply',
            ticket_id=ticket.id,
            details={'hasBody': bool(body.strip()), 'attachmentCount': len(media_items)},
        )
    message = TicketMessage(
        ticket_id=ticket.id,
        # Автор — тот, кто реально отправил: для admin/support это их user id
        # (session.context.user_id), не владелец тикета (#3029). Иначе
        # authorUserId в _message_snapshot врёт, а при нескольких сотрудниках
        # поддержки не установить, кто отвечал. Бот-путь пишет id админа же.
        user_id=session.context.user_id if is_from_admin else ticket.user_id,
        message_text=body,
        is_from_admin=is_from_admin,
        has_media=bool(primary),
        media_type=primary.get('type') if primary else None,
        media_file_id=primary.get('file_id') if primary else None,
        media_caption=primary.get('caption') if primary else None,
        media_items=media_items or None,
        created_at=_utc_now(),
    )
    db.add(message)
    # Mirror TicketCRUD.add_message: admin reply -> answered; user reply -> open and
    # re-arm SLA reminders from this message. Using 'pending' here diverged from every
    # other reply channel and left SLA reminders stale.
    if is_from_admin:
        ticket.status = 'answered'
    else:
        ticket.status = 'open'
        if hasattr(ticket, 'last_sla_reminder_at'):
            ticket.last_sla_reminder_at = None
    ticket.updated_at = _utc_now()
    await db.commit()
    await db.refresh(message)
    await db.refresh(ticket, ['messages', 'user'])

    media_file_id = primary.get('file_id') if primary else None
    media_type = primary.get('type') if primary else None
    try:
        if is_from_admin:
            notification = await TicketNotificationCRUD.create_user_notification_for_admin_reply(db, ticket, body)
            if notification:
                await notify_user_ticket_reply(ticket.user_id, ticket.id, body[:100])
        else:
            notification = await TicketNotificationCRUD.create_admin_notification_for_user_reply(db, ticket, body)
            if notification:
                await notify_admins_ticket_reply(ticket.id, body[:100], session.context.user_id)
    except Exception as exc:
        logger.warning('Support WS ticket notification creation failed', ticket_id=ticket.id, error=str(exc))

    # Telegram parity with the HTTP reply paths: admin replies DM the ticket owner,
    # user replies push to the admin bot chat. Without this, replies sent from the
    # mobile app are invisible to anyone monitoring support over Telegram.
    await _notify_ticket_reply_via_telegram(
        db, ticket, body, is_from_admin=is_from_admin, media_file_id=media_file_id, media_type=media_type
    )

    event = _message_event(
        'message.created',
        {'ticketId': str(ticket.id), 'message': _message_snapshot(message), 'ticketSnapshot': _ticket_snapshot(ticket)},
        ticket=ticket,
    )
    await support_ws_manager.broadcast_ticket_event(db, ticket, event)
    result = {
        'message': _message_snapshot(message),
        'ticketSnapshotVersion': _snapshot_version(ticket),
        'serverCursor': event['serverCursor'],
    }
    _store_idempotency_result(session, payload.get('idempotencyKey'), result)
    return result


async def _handle_ticket_mutation(
    db: AsyncSession,
    session: SupportWsSession,
    payload: dict[str, Any],
    *,
    command_name: str,
    field_name: str,
    allowed_values: set[str],
    event_name: str,
) -> dict[str, Any]:
    ticket_id = _parse_ticket_id(payload.get('ticketId'))
    ticket = await _get_visible_ticket(db, session.context, ticket_id)
    if ticket is None:
        raise LookupError(str(ticket_id))
    replay = _check_idempotency(session, payload.get('idempotencyKey'), payload)
    if replay is not None:
        return replay
    value = payload.get(field_name)
    if value not in allowed_values:
        raise ValueError(f'{field_name} must be one of: {sorted(allowed_values)}')

    previous = getattr(ticket, field_name if field_name != 'status' else 'status')
    if session.context.role == 'owner':
        await _log_ws_permission_audit(
            db,
            session,
            permission='tickets:close',
            command=command_name,
            status='denied',
            ticket_id=ticket.id,
            reason='owners cannot update ticket status or priority',
        )
        await db.commit()
        raise PermissionError('tickets:close is required')
    await _require_ws_permission(
        db,
        session,
        permission='tickets:close',
        command=command_name,
        ticket_id=ticket.id,
        details={'field': field_name, 'previousValue': previous, 'newValue': value},
    )
    setattr(ticket, field_name, value)
    ticket.updated_at = _utc_now()
    if field_name == 'status':
        ticket.closed_at = _utc_now() if value == 'closed' else None
    await db.commit()
    await db.refresh(ticket, ['messages', 'user'])

    event = _message_event(
        event_name,
        {
            'ticketId': str(ticket.id),
            f'previous{field_name[:1].upper()}{field_name[1:]}': previous,
            field_name: value,
            'ticketSnapshot': _ticket_snapshot(ticket),
        },
        ticket=ticket,
    )
    await support_ws_manager.broadcast_ticket_event(db, ticket, event)
    result = {'ticket': _ticket_snapshot(ticket, include_messages=True), 'serverCursor': event['serverCursor']}
    _store_idempotency_result(session, payload.get('idempotencyKey'), result)
    return result


async def _handle_state_reconcile(
    db: AsyncSession, session: SupportWsSession, payload: dict[str, Any]
) -> dict[str, Any]:
    if session.context.role != 'owner' and not await _has_permission(db, session.context, 'tickets:read'):
        raise PermissionError('tickets:read is required')
    client_tickets = payload.get('tickets') or []
    if not isinstance(client_tickets, list):
        raise ValueError('tickets must be an array')

    requested: dict[int, str | None] = {}
    for item in client_tickets:
        if not isinstance(item, dict):
            raise ValueError('tickets entries must be objects')
        ticket_id = _parse_ticket_id(item.get('ticketId'))
        requested[ticket_id] = item.get('snapshotVersion')

    changed: list[dict[str, Any]] = []
    removed: list[str] = []
    unchanged: list[dict[str, Any]] = []
    for ticket_id, known_version in requested.items():
        ticket = await _get_visible_ticket(db, session.context, ticket_id)
        if ticket is None:
            removed.append(str(ticket_id))
            continue
        current_version = _snapshot_version(ticket)
        if known_version == current_version:
            unchanged.append({'ticketId': str(ticket_id), 'changed': False, 'ticketSnapshot': None})
        else:
            changed.append({'ticketId': str(ticket_id), 'changed': True, 'ticketSnapshot': _ticket_snapshot(ticket)})

    return {
        'tickets': [*unchanged, *changed],
        'removedTicketIds': removed,
        'serverCursor': _server_cursor(*requested.keys(), _now_iso()),
        'asOf': _now_iso(),
    }


async def _upload_to_telegram(upload: UploadTransfer) -> dict[str, Any]:
    target_chat_id = _resolve_target_chat_id()
    input_file = BufferedInputFile(bytes(upload.chunks), filename=upload.file_name or 'upload')
    bot = create_bot()
    try:
        if upload.media_type == 'photo':
            message = await bot.send_photo(chat_id=target_chat_id, photo=input_file, disable_notification=True)
            media = message.photo[-1]
        elif upload.media_type == 'video':
            message = await bot.send_video(chat_id=target_chat_id, video=input_file, disable_notification=True)
            media = message.video
        else:
            message = await bot.send_document(chat_id=target_chat_id, document=input_file, disable_notification=True)
            media = message.document
        try:
            await bot.delete_message(chat_id=target_chat_id, message_id=message.message_id)
        except Exception:
            pass
        return {
            'mediaId': str(media.file_id),
            'fileUniqueId': getattr(media, 'file_unique_id', None),
            'type': upload.media_type,
            'fileName': upload.file_name,
            'contentType': upload.content_type,
            'sizeBytes': len(upload.chunks),
            'caption': None,
        }
    finally:
        await bot.session.close()


def _prune_expired_transfers(session: SupportWsSession) -> None:
    for key in [k for k, v in session.uploads.items() if v.expired or v.cancelled]:
        session.uploads.pop(key, None)
    for key in [k for k, v in session.downloads.items() if v.expired or v.cancelled]:
        session.downloads.pop(key, None)


def _assert_transfer_capacity(session: SupportWsSession) -> None:
    _prune_expired_transfers(session)
    if len(session.uploads) + len(session.downloads) >= MAX_ACTIVE_TRANSFERS:
        raise RuntimeError('RATE_LIMITED')


async def _handle_upload_begin(db: AsyncSession, session: SupportWsSession, payload: dict[str, Any]) -> dict[str, Any]:
    file_name = payload.get('fileName') or 'upload'
    content_type = payload.get('contentType')
    size_bytes = _parse_int(payload.get('sizeBytes'), 'sizeBytes', minimum=1, maximum=MAX_FILE_SIZE)
    media_type = _guess_media_type(file_name, content_type, payload.get('mediaType'))
    if media_type not in ALLOWED_MEDIA_TYPES:
        raise RuntimeError('UNSUPPORTED_MEDIA_TYPE')
    if _is_blocked_upload(file_name, content_type):
        raise RuntimeError('UNSUPPORTED_MEDIA_TYPE')
    # Uploads require an existing visible ticket (see docs: media upload/download
    # requires ticket/media visibility). A ticketless upload would let any account
    # stage arbitrary files through the support bot with no authorization anchor.
    parsed_ticket_id = _parse_ticket_id(payload.get('ticketId'))
    ticket = await _get_visible_ticket(db, session.context, parsed_ticket_id)
    if ticket is None:
        raise LookupError(str(parsed_ticket_id))
    _assert_transfer_capacity(session)
    upload_id = secrets.token_urlsafe(16)
    session.uploads[upload_id] = UploadTransfer(
        upload_id=upload_id,
        owner_user_id=session.context.user_id,
        ticket_id=parsed_ticket_id,
        media_type=media_type,
        file_name=str(file_name)[:128],
        content_type=str(content_type) if content_type else None,
        size_bytes=size_bytes,
        created_at=_utc_now(),
    )
    return {
        'uploadId': upload_id,
        'maxChunkBytes': MAX_UPLOAD_CHUNK_SIZE,
        'expiresAt': _iso(_utc_now() + timedelta(seconds=TRANSFER_TTL_SECONDS)),
    }


def _assert_transfer_owner(session: SupportWsSession, transfer: UploadTransfer | DownloadTransfer) -> None:
    if transfer.owner_user_id != session.context.user_id:
        raise RuntimeError('FORBIDDEN')


async def _handle_upload_chunk(session: SupportWsSession, payload: dict[str, Any]) -> dict[str, Any]:
    upload_id = payload.get('uploadId')
    upload = session.uploads.get(upload_id)
    if upload is None:
        raise RuntimeError('UPLOAD_NOT_FOUND')
    _assert_transfer_owner(session, upload)
    if upload.cancelled:
        raise RuntimeError('UPLOAD_CANCELLED')
    if upload.expired:
        raise RuntimeError('UPLOAD_EXPIRED')
    offset = _parse_int(payload.get('offset', len(upload.chunks)), 'offset', minimum=0)
    if offset != len(upload.chunks):
        raise ValueError('offset does not match current upload position')
    data = payload.get('data')
    if not isinstance(data, str):
        raise ValueError('data must be a base64 string')
    try:
        chunk = base64.b64decode(data.encode(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError('data must be valid base64') from exc
    if len(chunk) > MAX_UPLOAD_CHUNK_SIZE:
        raise RuntimeError('PAYLOAD_TOO_LARGE')
    if len(upload.chunks) + len(chunk) > upload.size_bytes or len(upload.chunks) + len(chunk) > MAX_FILE_SIZE:
        raise RuntimeError('PAYLOAD_TOO_LARGE')
    upload.chunks.extend(chunk)
    return {'uploadId': upload_id, 'receivedBytes': len(upload.chunks)}


async def _handle_upload_finish(session: SupportWsSession, payload: dict[str, Any]) -> dict[str, Any]:
    upload_id = payload.get('uploadId')
    upload = session.uploads.get(upload_id)
    if upload is None:
        raise RuntimeError('UPLOAD_NOT_FOUND')
    _assert_transfer_owner(session, upload)
    if upload.cancelled:
        raise RuntimeError('UPLOAD_CANCELLED')
    if upload.expired:
        raise RuntimeError('UPLOAD_EXPIRED')
    checksum = payload.get('sha256') or payload.get('checksumSha256')
    digest = hashlib.sha256(bytes(upload.chunks)).hexdigest()
    if checksum and checksum != digest:
        raise RuntimeError('UPLOAD_CHECKSUM_MISMATCH')
    if len(upload.chunks) != upload.size_bytes:
        raise ValueError('uploaded byte count does not match declared sizeBytes')
    media = await _upload_to_telegram(upload)
    media['sha256'] = digest
    media['ownerUserId'] = session.context.user_id
    session.completed_media[media['mediaId']] = media
    while len(session.completed_media) > MAX_COMPLETED_MEDIA:
        session.completed_media.pop(next(iter(session.completed_media)))
    session.uploads.pop(upload_id, None)
    return media


async def _handle_upload_cancel(session: SupportWsSession, payload: dict[str, Any]) -> dict[str, Any]:
    upload_id = payload.get('uploadId')
    upload = session.uploads.get(upload_id)
    if upload is None:
        raise RuntimeError('UPLOAD_NOT_FOUND')
    _assert_transfer_owner(session, upload)
    upload.cancelled = True
    session.uploads.pop(upload_id, None)
    return {'uploadId': upload_id, 'cancelled': True}


def _ticket_has_media(ticket: Ticket, media_id: str) -> bool:
    for message in ticket.messages or []:
        if str(message.media_file_id or '') == media_id:
            return True
        raw_items = getattr(message, 'media_items', None) or []
        if isinstance(raw_items, list) and any(
            str(item.get('file_id', '')) == media_id for item in raw_items if isinstance(item, dict)
        ):
            return True
    return False


async def _download_media_bytes(media_id: str) -> tuple[bytes, str, str, dict[str, str]]:
    bot = create_bot()
    try:
        file = await bot.get_file(media_id)
        if not file.file_path:
            raise RuntimeError('DOWNLOAD_NOT_FOUND')
        buffer = await bot.download_file(file.file_path)
        if hasattr(buffer, 'seek'):
            buffer.seek(0)
        content = buffer.read() if hasattr(buffer, 'read') else bytes(buffer)
        file_name = file.file_path.split('/')[-1]
        content_type, headers = _content_response_params(file_name)
        return content, file_name, content_type, headers
    finally:
        await bot.session.close()


async def _handle_download_begin(
    db: AsyncSession, session: SupportWsSession, payload: dict[str, Any]
) -> dict[str, Any]:
    media_id = payload.get('mediaId')
    if not isinstance(media_id, str) or not media_id:
        raise ValueError('mediaId is required')
    ticket_id = _parse_ticket_id(payload.get('ticketId'))
    ticket = await _get_visible_ticket(db, session.context, ticket_id)
    if ticket is None or not _ticket_has_media(ticket, media_id):
        raise RuntimeError('DOWNLOAD_NOT_FOUND')
    _assert_transfer_capacity(session)
    content, file_name, content_type, headers = await _download_media_bytes(media_id)
    download_id = secrets.token_urlsafe(16)
    session.downloads[download_id] = DownloadTransfer(
        download_id=download_id,
        owner_user_id=session.context.user_id,
        ticket_id=ticket_id,
        media_id=media_id,
        content=content,
        file_name=file_name,
        content_type=content_type,
        headers=headers,
        created_at=_utc_now(),
    )
    return {
        'downloadId': download_id,
        'mediaId': media_id,
        'sizeBytes': len(content),
        'chunkSize': DEFAULT_DOWNLOAD_CHUNK_SIZE,
        'sha256': hashlib.sha256(content).hexdigest(),
        'fileName': file_name,
        'contentType': content_type,
        'headers': headers,
    }


async def _handle_download_next(session: SupportWsSession, payload: dict[str, Any]) -> dict[str, Any]:
    download_id = payload.get('downloadId')
    download = session.downloads.get(download_id)
    if download is None or download.cancelled:
        raise RuntimeError('DOWNLOAD_NOT_FOUND')
    _assert_transfer_owner(session, download)
    if download.expired:
        session.downloads.pop(download_id, None)
        raise RuntimeError('DOWNLOAD_EXPIRED')
    chunk_size = _parse_int(
        payload.get('maxChunkBytes', DEFAULT_DOWNLOAD_CHUNK_SIZE),
        'maxChunkBytes',
        minimum=1,
        maximum=DEFAULT_DOWNLOAD_CHUNK_SIZE,
    )
    start = download.offset
    end = min(start + chunk_size, len(download.content))
    chunk = download.content[start:end]
    download.offset = end
    done = end >= len(download.content)
    if done:
        session.downloads.pop(download_id, None)
    return {
        'downloadId': download_id,
        'offset': start,
        'data': base64.b64encode(chunk).decode(),
        'done': done,
        'nextOffset': None if done else end,
    }


async def _handle_download_cancel(session: SupportWsSession, payload: dict[str, Any]) -> dict[str, Any]:
    download_id = payload.get('downloadId')
    download = session.downloads.get(download_id)
    if download is None:
        raise RuntimeError('DOWNLOAD_NOT_FOUND')
    _assert_transfer_owner(session, download)
    download.cancelled = True
    session.downloads.pop(download_id, None)
    return {'downloadId': download_id, 'cancelled': True}


async def _handle_reauthenticate(
    db: AsyncSession, session: SupportWsSession, payload: dict[str, Any]
) -> dict[str, Any]:
    access_token = payload.get('accessToken')
    if not isinstance(access_token, str) or not access_token:
        raise ValueError('accessToken is required')
    context, error = await _authenticate_ws(db, session.websocket, access_token=access_token)
    if error is not None or context is None:
        raise RuntimeError(error['code'] if error else 'AUTH_REQUIRED')
    if context.user_id != session.context.user_id:
        raise RuntimeError('FORBIDDEN')
    session.context = context
    return {
        'authenticated': True,
        'userId': str(context.user_id),
        'role': context.role,
        'accessTokenExpiresAt': _iso(datetime.fromtimestamp(context.exp_timestamp, UTC))
        if context.exp_timestamp
        else None,
    }


async def _dispatch_command(
    db: AsyncSession, session: SupportWsSession, command: str, payload: dict[str, Any]
) -> dict[str, Any]:
    if command == 'ticket.list':
        return await _handle_ticket_list(db, session, payload)
    if command == 'ticket.detail':
        return await _handle_ticket_detail(db, session, payload)
    if command == 'ticket.reply':
        return await _handle_ticket_reply(db, session, payload)
    if command == 'ticket.status.update':
        return await _handle_ticket_mutation(
            db,
            session,
            payload,
            command_name='ticket.status.update',
            field_name='status',
            allowed_values=ALLOWED_STATUSES,
            event_name='ticket.status.updated',
        )
    if command == 'ticket.priority.update':
        return await _handle_ticket_mutation(
            db,
            session,
            payload,
            command_name='ticket.priority.update',
            field_name='priority',
            allowed_values=ALLOWED_PRIORITIES,
            event_name='ticket.priority.updated',
        )
    if command == 'state.reconcile':
        return await _handle_state_reconcile(db, session, payload)
    if command == 'media.upload.begin':
        return await _handle_upload_begin(db, session, payload)
    if command == 'media.upload.chunk':
        return await _handle_upload_chunk(session, payload)
    if command == 'media.upload.finish':
        return await _handle_upload_finish(session, payload)
    if command == 'media.upload.cancel':
        return await _handle_upload_cancel(session, payload)
    if command == 'media.download.begin':
        return await _handle_download_begin(db, session, payload)
    if command == 'media.download.next':
        return await _handle_download_next(session, payload)
    if command == 'media.download.cancel':
        return await _handle_download_cancel(session, payload)
    if command == 'auth.reauthenticate':
        return await _handle_reauthenticate(db, session, payload)
    raise ValueError(f'Unsupported command: {command}')


def _map_exception(command: str, exc: Exception) -> dict[str, Any]:
    message = str(exc) or 'Command failed'
    if isinstance(exc, PermissionError):
        return _shared_error('FORBIDDEN', message, resource_type='ticket')
    if isinstance(exc, LookupError):
        resource_type = 'media' if command.startswith('media.') else 'ticket'
        return _shared_error('NOT_FOUND', 'Resource not found', resource_type=resource_type, resource_id=message)
    if isinstance(exc, ValueError):
        return _shared_error('VALIDATION_ERROR', message, resource_type=None)
    if isinstance(exc, RuntimeError) and message in ERROR_CODES:
        resource_type = (
            'upload' if message.startswith('UPLOAD_') else 'download' if message.startswith('DOWNLOAD_') else 'ticket'
        )
        return _shared_error(message, message.replace('_', ' ').title(), resource_type=resource_type)
    logger.exception('Support WS command failed', command=command, error=message)
    return _shared_error('INTERNAL_ERROR', 'Internal support websocket error', retryable=True)


def _validate_envelope(message: Any) -> tuple[str, str, dict[str, Any]]:
    if not isinstance(message, dict):
        raise ValueError('command envelope must be a JSON object')
    if message.get('type') != 'command':
        raise ValueError('type must be command')
    command = message.get('command')
    request_id = message.get('requestId')
    payload = message.get('payload', {})
    if not isinstance(command, str) or not command:
        raise ValueError('command is required')
    if not isinstance(request_id, str) or not request_id:
        raise ValueError('requestId is required')
    if not isinstance(payload, dict):
        raise ValueError('payload must be an object')
    return command, request_id, payload


async def _send_auth_expiring_notice(session: SupportWsSession) -> None:
    exp = session.context.exp_timestamp
    if exp is None:
        return
    seconds_left = exp - int(_utc_now().timestamp())
    if seconds_left <= 0:
        await session.websocket.close(code=1008, reason='Access token expired')
        return
    if seconds_left <= AUTH_EXPIRING_NOTICE_SECONDS:
        await session.send_json(
            _message_event(
                'auth.expiring',
                {
                    'expiresAt': _iso(datetime.fromtimestamp(exp, UTC)),
                    'retryAfterMs': None,
                },
            )
        )


async def _reject_upgrade(websocket: WebSocket, code: int, reason: str) -> None:
    logger.debug('Support WS upgrade rejected', code=code, reason=reason)
    await websocket.close(code=1008, reason=reason)


@router.websocket('/ws/support/v1')
async def support_mobile_websocket_endpoint(websocket: WebSocket):
    """Mobile-only support ticket command WebSocket."""
    if websocket.query_params.get('token') or websocket.query_params.get('api_key'):
        await _reject_upgrade(websocket, 400, 'Query-token auth is not supported')
        return

    if SUPPORTED_SUBPROTOCOL not in _subprotocols(websocket):
        await _reject_upgrade(websocket, 426, 'Unsupported WebSocket subprotocol')
        return

    async with AsyncSessionLocal() as db:
        context, error = await _authenticate_ws(db, websocket)
    if error is not None or context is None:
        await _reject_upgrade(websocket, 401, error['code'] if error else 'AUTH_REQUIRED')
        return

    await websocket.accept(subprotocol=SUPPORTED_SUBPROTOCOL)
    session = SupportWsSession(websocket=websocket, context=context)
    await support_ws_manager.connect(session)
    try:
        await session.send_json(
            _message_event(
                'connection.ready',
                {
                    'userId': str(context.user_id),
                    'role': context.role,
                    'protocol': SUPPORTED_SUBPROTOCOL,
                    'ticketCreate': {'supported': False, 'reason': 'mobile_admin_support_scope_excludes_ticket_create'},
                    'mediaDownload': {'transport': 'websocket'},
                    'assignment': {'assignedTo': None, 'previousAssignedTo': None},
                },
            )
        )
        await _send_auth_expiring_notice(session)

        while True:
            try:
                raw = await websocket.receive_text()
                if len(raw) > MAX_MESSAGE_BYTES:
                    await session.send_json(
                        _command_result(
                            'unknown',
                            '',
                            error=_shared_error('PAYLOAD_TOO_LARGE', 'message exceeds maximum frame size'),
                        )
                    )
                    continue
                message = json.loads(raw)
                command, request_id, payload = _validate_envelope(message)
                if command != 'auth.reauthenticate':
                    await _send_auth_expiring_notice(session)
                    exp = session.context.exp_timestamp
                    if exp is not None and exp <= int(_utc_now().timestamp()):
                        await session.send_json(
                            _command_result(
                                command,
                                request_id,
                                error=_shared_error('AUTH_EXPIRED', 'Access token expired', resource_type='auth'),
                            )
                        )
                        await websocket.close(code=1008, reason='Access token expired')
                        return
                async with AsyncSessionLocal() as db:
                    result = await _dispatch_command(db, session, command, payload)
                await session.send_json(_command_result(command, request_id, payload=result))
            except json.JSONDecodeError:
                await session.send_json(
                    _command_result(
                        'unknown',
                        '',
                        error=_shared_error('VALIDATION_ERROR', 'message must be valid JSON'),
                    )
                )
            except WebSocketDisconnect:
                break
            except Exception as exc:
                try:
                    command = command if 'command' in locals() else 'unknown'
                    request_id = request_id if 'request_id' in locals() else ''
                    await session.send_json(_command_result(command, request_id, error=_map_exception(command, exc)))
                except Exception:
                    logger.exception('Support WS failed to send command error')
                    break
    finally:
        await support_ws_manager.disconnect(session)
        if websocket.client_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.close()
            except RuntimeError:
                pass
