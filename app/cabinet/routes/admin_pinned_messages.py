"""Admin routes for pinned messages in cabinet."""

import time
from datetime import UTC, datetime

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PinnedMessage, User
from app.services.pinned_message_service import (
    broadcast_pinned_message,
    deactivate_active_pinned_message,
    get_active_pinned_message,
    set_active_pinned_message,
    unpin_active_pinned_message,
)
from app.utils.validators import sanitize_html, validate_html_tags

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.pinned_messages import (
    PinnedMessageBroadcastResponse,
    PinnedMessageCreateRequest,
    PinnedMessageListResponse,
    PinnedMessageResponse,
    PinnedMessageSettingsRequest,
    PinnedMessageUnpinResponse,
    PinnedMessageUpdateRequest,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/pinned-messages', tags=['Cabinet Admin Pinned Messages'])

# Broadcast cooldown: min 60 seconds between mass operations
_BROADCAST_COOLDOWN_SECONDS = 60
_last_broadcast_time: float = 0.0


def _check_broadcast_cooldown() -> None:
    global _last_broadcast_time
    now = time.monotonic()
    elapsed = now - _last_broadcast_time
    if _last_broadcast_time > 0 and elapsed < _BROADCAST_COOLDOWN_SECONDS:
        remaining = int(_BROADCAST_COOLDOWN_SECONDS - elapsed)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f'Broadcast cooldown active. Try again in {remaining} seconds.',
        )
    _last_broadcast_time = now


def _serialize_pinned_message(msg: PinnedMessage) -> PinnedMessageResponse:
    return PinnedMessageResponse(
        id=msg.id,
        content=msg.content,
        media_type=msg.media_type,
        media_file_id=msg.media_file_id,
        send_before_menu=msg.send_before_menu,
        send_on_every_start=msg.send_on_every_start,
        is_active=msg.is_active,
        created_by=msg.created_by,
        created_at=msg.created_at,
        updated_at=msg.updated_at,
    )


_cached_bot: Bot | None = None


def _get_bot() -> Bot:
    global _cached_bot
    if _cached_bot is None:
        _cached_bot = Bot(
            token=settings.BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
    return _cached_bot


# ============ List / Get Endpoints ============


@router.get('', response_model=PinnedMessageListResponse)
async def list_pinned_messages(
    admin: User = Depends(require_permission('pinned_messages:read')),
    db: AsyncSession = Depends(get_cabinet_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    active_only: bool = Query(False),
) -> PinnedMessageListResponse:
    """Get list of pinned messages with pagination."""
    query = select(PinnedMessage).order_by(PinnedMessage.created_at.desc())
    count_query = select(func.count(PinnedMessage.id))

    if active_only:
        query = query.where(PinnedMessage.is_active.is_(True))
        count_query = count_query.where(PinnedMessage.is_active.is_(True))

    total = await db.scalar(count_query) or 0
    result = await db.execute(query.offset(offset).limit(limit))
    items = result.scalars().all()

    return PinnedMessageListResponse(
        items=[_serialize_pinned_message(msg) for msg in items],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get('/active', response_model=PinnedMessageResponse | None)
async def get_active_message(
    admin: User = Depends(require_permission('pinned_messages:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PinnedMessageResponse | None:
    """Get current active pinned message."""
    msg = await get_active_pinned_message(db)
    if not msg:
        return None
    return _serialize_pinned_message(msg)


@router.get('/{message_id}', response_model=PinnedMessageResponse)
async def get_pinned_message(
    message_id: int,
    admin: User = Depends(require_permission('pinned_messages:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PinnedMessageResponse:
    """Get pinned message by ID."""
    result = await db.execute(select(PinnedMessage).where(PinnedMessage.id == message_id))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Pinned message not found')
    return _serialize_pinned_message(msg)


# ============ Create / Update Endpoints ============


@router.post('', response_model=PinnedMessageBroadcastResponse, status_code=status.HTTP_201_CREATED)
async def create_pinned_message(
    payload: PinnedMessageCreateRequest,
    admin: User = Depends(require_permission('pinned_messages:create')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PinnedMessageBroadcastResponse:
    """
    Create a new pinned message.

    Automatically deactivates previous active message.
    If broadcast=true, sends to all active users immediately.
    """
    # Проверяем cooldown ДО мутации в БД
    if payload.broadcast:
        _check_broadcast_cooldown()

    content = payload.content.strip()
    if not content and not payload.media:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Either content or media must be provided')

    media_type = payload.media.type if payload.media else None
    media_file_id = payload.media.file_id if payload.media else None

    try:
        msg = await set_active_pinned_message(
            db=db,
            content=content,
            created_by=admin.id,
            media_type=media_type,
            media_file_id=media_file_id,
            send_before_menu=payload.send_before_menu,
            send_on_every_start=payload.send_on_every_start,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    sent_count = 0
    failed_count = 0

    if payload.broadcast:
        sent_count, failed_count = await broadcast_pinned_message(_get_bot(), db, msg)

    logger.info(
        'Admin created pinned message # (broadcast=)', admin_id=admin.id, message_id=msg.id, broadcast=payload.broadcast
    )

    return PinnedMessageBroadcastResponse(
        message=_serialize_pinned_message(msg),
        sent_count=sent_count,
        failed_count=failed_count,
    )


@router.patch('/{message_id}', response_model=PinnedMessageResponse)
async def update_pinned_message(
    message_id: int,
    payload: PinnedMessageUpdateRequest,
    admin: User = Depends(require_permission('pinned_messages:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PinnedMessageResponse:
    """Update a pinned message content, media, or settings."""
    result = await db.execute(select(PinnedMessage).where(PinnedMessage.id == message_id))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Pinned message not found')

    if payload.content is not None:
        sanitized = sanitize_html(payload.content)
        is_valid, error = validate_html_tags(sanitized)
        if not is_valid:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, error)
        msg.content = sanitized

    if payload.media is not None:
        msg.media_type = payload.media.type
        msg.media_file_id = payload.media.file_id

    if payload.send_before_menu is not None:
        msg.send_before_menu = payload.send_before_menu

    if payload.send_on_every_start is not None:
        msg.send_on_every_start = payload.send_on_every_start

    msg.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(msg)

    logger.info('Admin updated pinned message #', admin_id=admin.id, message_id=message_id)

    return _serialize_pinned_message(msg)


@router.patch('/{message_id}/settings', response_model=PinnedMessageResponse)
async def update_pinned_message_settings(
    message_id: int,
    payload: PinnedMessageSettingsRequest,
    admin: User = Depends(require_permission('pinned_messages:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PinnedMessageResponse:
    """Update only pinned message display settings."""
    result = await db.execute(select(PinnedMessage).where(PinnedMessage.id == message_id))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Pinned message not found')

    if payload.send_before_menu is not None:
        msg.send_before_menu = payload.send_before_menu

    if payload.send_on_every_start is not None:
        msg.send_on_every_start = payload.send_on_every_start

    msg.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(msg)

    return _serialize_pinned_message(msg)


# ============ Active Message Actions (before /{message_id} POST routes) ============


@router.post('/active/deactivate', response_model=PinnedMessageResponse | None)
async def deactivate_active_message(
    admin: User = Depends(require_permission('pinned_messages:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PinnedMessageResponse | None:
    """Deactivate the current active pinned message without unpinning from users."""
    msg = await deactivate_active_pinned_message(db)
    if not msg:
        return None

    logger.info('Admin deactivated pinned message #', admin_id=admin.id, message_id=msg.id)

    return _serialize_pinned_message(msg)


@router.post('/active/unpin', response_model=PinnedMessageUnpinResponse)
async def unpin_active_message(
    admin: User = Depends(require_permission('pinned_messages:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PinnedMessageUnpinResponse:
    """Unpin messages from all users and deactivate the active pinned message."""
    _check_broadcast_cooldown()
    unpinned_count, failed_count, was_active = await unpin_active_pinned_message(_get_bot(), db)

    if was_active:
        logger.info(
            'Admin unpinned active message: unpinned=, failed',
            admin_id=admin.id,
            unpinned_count=unpinned_count,
            failed_count=failed_count,
        )

    return PinnedMessageUnpinResponse(
        unpinned_count=unpinned_count,
        failed_count=failed_count,
        was_active=was_active,
    )


# ============ Per-Message Actions ============


@router.post('/{message_id}/activate', response_model=PinnedMessageBroadcastResponse)
async def activate_pinned_message(
    message_id: int,
    broadcast: bool = Query(False),
    admin: User = Depends(require_permission('pinned_messages:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PinnedMessageBroadcastResponse:
    """
    Activate a pinned message.

    Deactivates the current active message and activates the specified one.
    If broadcast=true, sends to all active users immediately.
    """
    # Проверяем cooldown ДО мутации в БД
    if broadcast:
        _check_broadcast_cooldown()

    result = await db.execute(select(PinnedMessage).where(PinnedMessage.id == message_id))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Pinned message not found')

    await db.execute(
        update(PinnedMessage)
        .where(PinnedMessage.is_active.is_(True))
        .values(is_active=False, updated_at=datetime.now(UTC))
    )

    msg.is_active = True
    msg.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(msg)

    sent_count = 0
    failed_count = 0

    if broadcast:
        sent_count, failed_count = await broadcast_pinned_message(_get_bot(), db, msg)

    logger.info(
        'Admin activated pinned message # (broadcast=)', admin_id=admin.id, message_id=message_id, broadcast=broadcast
    )

    return PinnedMessageBroadcastResponse(
        message=_serialize_pinned_message(msg),
        sent_count=sent_count,
        failed_count=failed_count,
    )


@router.post('/{message_id}/broadcast', response_model=PinnedMessageBroadcastResponse)
async def broadcast_message(
    message_id: int,
    admin: User = Depends(require_permission('pinned_messages:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PinnedMessageBroadcastResponse:
    """Broadcast a pinned message to all active users."""
    _check_broadcast_cooldown()

    result = await db.execute(select(PinnedMessage).where(PinnedMessage.id == message_id))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Pinned message not found')

    sent_count, failed_count = await broadcast_pinned_message(_get_bot(), db, msg)

    logger.info(
        'Admin broadcast pinned message #: sent=, failed',
        admin_id=admin.id,
        message_id=message_id,
        sent_count=sent_count,
        failed_count=failed_count,
    )

    return PinnedMessageBroadcastResponse(
        message=_serialize_pinned_message(msg),
        sent_count=sent_count,
        failed_count=failed_count,
    )


@router.delete('/{message_id}', status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_pinned_message(
    message_id: int,
    admin: User = Depends(require_permission('pinned_messages:delete')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> None:
    """Delete a pinned message. Active messages must be deactivated first."""
    result = await db.execute(select(PinnedMessage).where(PinnedMessage.id == message_id))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Pinned message not found')

    if msg.is_active:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            'Cannot delete active pinned message. Deactivate it first.',
        )

    await db.delete(msg)
    await db.commit()

    logger.info('Admin deleted pinned message #', admin_id=admin.id, message_id=message_id)
