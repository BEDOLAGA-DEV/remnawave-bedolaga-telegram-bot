from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Optional

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.localization.texts import get_texts
from app.database.models import PinnedMessage
from app.services.pinned_message_service import (
    broadcast_pinned_message,
    deactivate_active_pinned_message,
    get_active_pinned_message,
    set_active_pinned_message,
    unpin_active_pinned_message,
)

from ..dependencies import get_db_session, require_api_token
from ..schemas.pinned_messages import (
    PinnedMessageBroadcastResponse,
    PinnedMessageCreateRequest,
    PinnedMessageListResponse,
    PinnedMessageResponse,
    PinnedMessageSettingsRequest,
    PinnedMessageUnpinResponse,
    PinnedMessageUpdateRequest,
)


router = APIRouter()


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


def _get_bot() -> Bot:
    """Создать экземпляр бота для API операций."""
    return Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


@router.get('', response_model=PinnedMessageListResponse)
async def list_pinned_messages(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    active_only: bool = Query(False),
    token: Any = Depends(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PinnedMessageListResponse:
    """Получить список всех закреплённых сообщений."""
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
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get('/active', response_model=Optional[PinnedMessageResponse])
async def get_active_message(
    token: Any = Depends(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PinnedMessageResponse | None:
    """Получить текущее активное закреплённое сообщение."""
    msg = await get_active_pinned_message(db)
    if not msg:
        return None
    return _serialize_pinned_message(msg)


@router.get('/{message_id}', response_model=PinnedMessageResponse)
async def get_pinned_message(
    message_id: int,
    token: Any = Depends(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PinnedMessageResponse:
    """Получить закреплённое сообщение по ID."""
    result = await db.execute(select(PinnedMessage).where(PinnedMessage.id == message_id))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t(
                'WEBAPI_PINNED_MESSAGES_NOT_FOUND',
                'Pinned message not found',
            ),
        )
    return _serialize_pinned_message(msg)


@router.post('', response_model=PinnedMessageBroadcastResponse, status_code=status.HTTP_201_CREATED)
async def create_pinned_message(
    payload: PinnedMessageCreateRequest,
    broadcast: bool = Query(
        False, description='Разослать сообщение всем пользователям (по умолчанию False — только при /start)'
    ),
    token: Any = Depends(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PinnedMessageBroadcastResponse:
    """
    Создать новое закреплённое сообщение.

    Автоматически деактивирует предыдущее активное сообщение.
    - broadcast=False (по умолчанию): пользователи увидят при следующем /start
    - broadcast=True: рассылает сообщение всем активным пользователям сразу
    """
    content = payload.content.strip()
    if not content and not payload.media:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=get_texts('ru').t(
                'WEBAPI_PINNED_MESSAGES_CONTENT_OR_MEDIA_REQUIRED',
                'Either content or media must be provided',
            ),
        )

    media_type = payload.media.type if payload.media else None
    media_file_id = payload.media.file_id if payload.media else None

    try:
        msg = await set_active_pinned_message(
            db=db,
            content=content,
            created_by=None,
            media_type=media_type,
            media_file_id=media_file_id,
            send_before_menu=payload.send_before_menu,
            send_on_every_start=payload.send_on_every_start,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    sent_count = 0
    failed_count = 0

    if broadcast:
        sent_count, failed_count = await broadcast_pinned_message(_get_bot(), db, msg)

    return PinnedMessageBroadcastResponse(
        message=_serialize_pinned_message(msg),
        sent_count=sent_count,
        failed_count=failed_count,
    )


@router.patch('/{message_id}', response_model=PinnedMessageResponse)
async def update_pinned_message(
    message_id: int,
    payload: PinnedMessageUpdateRequest,
    token: Any = Depends(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PinnedMessageResponse:
    """
    Обновить закреплённое сообщение.

    Можно обновить контент, медиа и настройки показа.
    Не делает рассылку — для рассылки используйте POST /pinned-messages/{id}/broadcast.
    """
    result = await db.execute(select(PinnedMessage).where(PinnedMessage.id == message_id))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t(
                'WEBAPI_PINNED_MESSAGES_NOT_FOUND',
                'Pinned message not found',
            ),
        )

    if payload.content is not None:
        from app.utils.validators import sanitize_html, validate_html_tags

        sanitized = sanitize_html(payload.content)
        is_valid, error = validate_html_tags(sanitized)
        if not is_valid:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, error)
        msg.content = sanitized

    if payload.media is not None:
        if payload.media.type not in ('photo', 'video'):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=get_texts('ru').t(
                    'WEBAPI_PINNED_MESSAGES_ONLY_PHOTO_OR_VIDEO_SUPPORTED',
                    'Only photo or video media types are supported',
                ),
            )
        msg.media_type = payload.media.type
        msg.media_file_id = payload.media.file_id

    if payload.send_before_menu is not None:
        msg.send_before_menu = payload.send_before_menu

    if payload.send_on_every_start is not None:
        msg.send_on_every_start = payload.send_on_every_start

    msg.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(msg)

    return _serialize_pinned_message(msg)


@router.patch('/{message_id}/settings', response_model=PinnedMessageResponse)
async def update_pinned_message_settings(
    message_id: int,
    payload: PinnedMessageSettingsRequest,
    token: Any = Depends(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PinnedMessageResponse:
    """
    Обновить только настройки закреплённого сообщения.

    - send_before_menu: показывать до или после меню
    - send_on_every_start: показывать при каждом /start или только один раз
    """
    result = await db.execute(select(PinnedMessage).where(PinnedMessage.id == message_id))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t(
                'WEBAPI_PINNED_MESSAGES_NOT_FOUND',
                'Pinned message not found',
            ),
        )

    if payload.send_before_menu is not None:
        msg.send_before_menu = payload.send_before_menu

    if payload.send_on_every_start is not None:
        msg.send_on_every_start = payload.send_on_every_start

    msg.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(msg)

    return _serialize_pinned_message(msg)


@router.post('/{message_id}/activate', response_model=PinnedMessageBroadcastResponse)
async def activate_pinned_message(
    message_id: int,
    broadcast: bool = Query(
        False, description='Разослать сообщение всем пользователям (по умолчанию False — только при /start)'
    ),
    token: Any = Depends(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PinnedMessageBroadcastResponse:
    """
    Активировать закреплённое сообщение.

    Деактивирует текущее активное сообщение и активирует указанное.
    - broadcast=False (по умолчанию): пользователи увидят при следующем /start
    - broadcast=True: рассылает сообщение всем активным пользователям сразу
    """
    result = await db.execute(select(PinnedMessage).where(PinnedMessage.id == message_id))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t(
                'WEBAPI_PINNED_MESSAGES_NOT_FOUND',
                'Pinned message not found',
            ),
        )

    # Деактивируем все активные
    await db.execute(
        update(PinnedMessage)
        .where(PinnedMessage.is_active.is_(True))
        .values(is_active=False, updated_at=datetime.now(UTC))
    )

    # Активируем указанное
    msg.is_active = True
    msg.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(msg)

    sent_count = 0
    failed_count = 0

    if broadcast:
        sent_count, failed_count = await broadcast_pinned_message(_get_bot(), db, msg)

    return PinnedMessageBroadcastResponse(
        message=_serialize_pinned_message(msg),
        sent_count=sent_count,
        failed_count=failed_count,
    )


@router.post('/{message_id}/broadcast', response_model=PinnedMessageBroadcastResponse)
async def broadcast_message(
    message_id: int,
    token: Any = Depends(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PinnedMessageBroadcastResponse:
    """
    Разослать закреплённое сообщение всем активным пользователям.

    Работает для любого сообщения, независимо от его статуса активности.
    """
    result = await db.execute(select(PinnedMessage).where(PinnedMessage.id == message_id))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t(
                'WEBAPI_PINNED_MESSAGES_NOT_FOUND',
                'Pinned message not found',
            ),
        )

    sent_count, failed_count = await broadcast_pinned_message(_get_bot(), db, msg)

    return PinnedMessageBroadcastResponse(
        message=_serialize_pinned_message(msg),
        sent_count=sent_count,
        failed_count=failed_count,
    )


@router.post('/active/deactivate', response_model=Optional[PinnedMessageResponse])
async def deactivate_active_message(
    token: Any = Depends(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PinnedMessageResponse | None:
    """
    Деактивировать текущее активное закреплённое сообщение.

    Не удаляет сообщение и не открепляет у пользователей.
    """
    msg = await deactivate_active_pinned_message(db)
    if not msg:
        return None
    return _serialize_pinned_message(msg)


@router.post('/active/unpin', response_model=PinnedMessageUnpinResponse)
async def unpin_active_message(
    token: Any = Depends(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PinnedMessageUnpinResponse:
    """
    Открепить сообщение у всех пользователей и деактивировать.

    Удаляет закреплённое сообщение из чатов всех активных пользователей.
    """
    unpinned_count, failed_count, was_active = await unpin_active_pinned_message(_get_bot(), db)
    return PinnedMessageUnpinResponse(
        unpinned_count=unpinned_count,
        failed_count=failed_count,
        was_active=was_active,
    )


@router.delete('/{message_id}', status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_pinned_message(
    message_id: int,
    token: Any = Depends(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """
    Удалить закреплённое сообщение.

    Если сообщение активно, сначала будет деактивировано.
    Не открепляет сообщение у пользователей — для этого используйте /active/unpin.
    """
    result = await db.execute(select(PinnedMessage).where(PinnedMessage.id == message_id))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t(
                'WEBAPI_PINNED_MESSAGES_NOT_FOUND',
                'Pinned message not found',
            ),
        )

    await db.delete(msg)
    await db.commit()
