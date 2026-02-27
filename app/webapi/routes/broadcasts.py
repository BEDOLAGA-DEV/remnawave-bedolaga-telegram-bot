from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import BroadcastHistory
from app.localization.texts import get_texts
from app.services.broadcast_service import (
    BroadcastConfig,
    BroadcastMediaConfig,
    broadcast_service,
)

from ..dependencies import get_db_session, require_api_token
from ..schemas.broadcasts import (
    BroadcastCreateRequest,
    BroadcastListResponse,
    BroadcastResponse,
)


router = APIRouter()


def _serialize_broadcast(broadcast: BroadcastHistory) -> BroadcastResponse:
    return BroadcastResponse(
        id=broadcast.id,
        target_type=broadcast.target_type,
        message_text=broadcast.message_text,
        has_media=broadcast.has_media,
        media_type=broadcast.media_type,
        media_file_id=broadcast.media_file_id,
        media_caption=broadcast.media_caption,
        total_count=broadcast.total_count,
        sent_count=broadcast.sent_count,
        failed_count=broadcast.failed_count,
        blocked_count=broadcast.blocked_count or 0,
        status=broadcast.status,
        admin_id=broadcast.admin_id,
        admin_name=broadcast.admin_name,
        created_at=broadcast.created_at,
        completed_at=broadcast.completed_at,
    )


@router.post('', response_model=BroadcastResponse, status_code=status.HTTP_201_CREATED)
async def create_broadcast(
    payload: BroadcastCreateRequest,
    token: Any = Depends(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> BroadcastResponse:
    texts = get_texts('ru')
    message_text = payload.message_text.strip()
    if not message_text:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t('WEBAPI_BROADCASTS_MESSAGE_TEXT_MUST_NOT_BE_EMPTY', 'Message text must not be empty'),
        )

    media_payload = payload.media

    broadcast = BroadcastHistory(
        target_type=payload.target,
        message_text=message_text,
        has_media=media_payload is not None,
        media_type=media_payload.type if media_payload else None,
        media_file_id=media_payload.file_id if media_payload else None,
        media_caption=media_payload.caption if media_payload else None,
        total_count=0,
        sent_count=0,
        failed_count=0,
        status='queued',
        admin_id=None,
        admin_name=getattr(token, 'name', None) or getattr(token, 'created_by', None),
    )
    db.add(broadcast)
    await db.commit()
    await db.refresh(broadcast)

    media_config = None
    if media_payload:
        media_config = BroadcastMediaConfig(
            type=media_payload.type,
            file_id=media_payload.file_id,
            caption=media_payload.caption or message_text,
        )

    config = BroadcastConfig(
        target=payload.target,
        message_text=message_text,
        selected_buttons=payload.selected_buttons,
        media=media_config,
        initiator_name=getattr(token, 'name', None) or getattr(token, 'created_by', None),
    )

    await broadcast_service.start_broadcast(broadcast.id, config)
    await db.refresh(broadcast)

    return _serialize_broadcast(broadcast)


@router.get('', response_model=BroadcastListResponse)
async def list_broadcasts(
    _: Any = Depends(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> BroadcastListResponse:
    total = await db.scalar(select(func.count(BroadcastHistory.id))) or 0

    result = await db.execute(
        select(BroadcastHistory).order_by(BroadcastHistory.created_at.desc()).offset(offset).limit(limit)
    )
    broadcasts = result.scalars().all()

    return BroadcastListResponse(
        items=[_serialize_broadcast(item) for item in broadcasts],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.post('/{broadcast_id}/stop', response_model=BroadcastResponse)
async def stop_broadcast(
    broadcast_id: int,
    _: Any = Depends(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> BroadcastResponse:
    texts = get_texts('ru')
    broadcast = await db.get(BroadcastHistory, broadcast_id)
    if not broadcast:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_BROADCASTS_NOT_FOUND', 'Broadcast not found'),
        )

    if broadcast.status not in {'queued', 'in_progress', 'cancelling'}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t('WEBAPI_BROADCASTS_IS_NOT_RUNNING', 'Broadcast is not running'),
        )

    is_running = await broadcast_service.request_stop(broadcast_id)

    if is_running:
        broadcast.status = 'cancelling'
    else:
        broadcast.status = 'cancelled'
        broadcast.completed_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(broadcast)

    return _serialize_broadcast(broadcast)
