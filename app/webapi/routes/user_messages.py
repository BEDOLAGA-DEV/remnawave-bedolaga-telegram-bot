from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, Security, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.localization.texts import get_texts
from app.database.crud.user_message import (
    create_user_message,
    delete_user_message,
    get_all_user_messages,
    get_user_message_by_id,
    get_user_messages_count,
    toggle_user_message_status,
    update_user_message,
)

from ..dependencies import get_db_session, require_api_token
from ..schemas.user_messages import (
    UserMessageCreateRequest,
    UserMessageListResponse,
    UserMessageResponse,
    UserMessageUpdateRequest,
)


router = APIRouter()


def _serialize(message) -> UserMessageResponse:
    return UserMessageResponse(
        id=message.id,
        message_text=message.message_text,
        is_active=message.is_active,
        sort_order=message.sort_order,
        created_by=message.created_by,
        created_at=message.created_at,
        updated_at=message.updated_at,
    )


@router.get('', response_model=UserMessageListResponse)
async def list_user_messages(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    include_inactive: bool = Query(True, description='Включать неактивные сообщения'),
) -> UserMessageListResponse:
    total = await get_user_messages_count(db, include_inactive=include_inactive)
    messages = await get_all_user_messages(
        db,
        offset=offset,
        limit=limit,
        include_inactive=include_inactive,
    )

    return UserMessageListResponse(
        items=[_serialize(message) for message in messages],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post('', response_model=UserMessageResponse, status_code=status.HTTP_201_CREATED)
async def create_user_message_endpoint(
    payload: UserMessageCreateRequest,
    token: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserMessageResponse:
    created_by = getattr(token, 'id', None)
    try:
        message = await create_user_message(
            db,
            message_text=payload.message_text,
            created_by=created_by,
            is_active=payload.is_active,
            sort_order=payload.sort_order,
        )
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error

    return _serialize(message)


@router.patch('/{message_id}', response_model=UserMessageResponse)
async def update_user_message_endpoint(
    message_id: int,
    payload: UserMessageUpdateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserMessageResponse:
    update_payload = payload.dict(exclude_unset=True)
    try:
        message = await update_user_message(db, message_id, **update_payload)
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error

    if not message:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_USER_MESSAGES_NOT_FOUND', 'User message not found'),
        )

    return _serialize(message)


@router.post('/{message_id}/toggle', response_model=UserMessageResponse)
async def toggle_user_message_endpoint(
    message_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserMessageResponse:
    message = await toggle_user_message_status(db, message_id)

    if not message:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_USER_MESSAGES_NOT_FOUND', 'User message not found'),
        )

    return _serialize(message)


@router.delete('/{message_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_message_endpoint(
    message_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    message = await get_user_message_by_id(db, message_id)
    if not message:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_USER_MESSAGES_NOT_FOUND', 'User message not found'),
        )

    await delete_user_message(db, message_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
