from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, Security, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.localization.texts import get_texts
from app.database.crud.welcome_text import (
    count_welcome_texts,
    create_welcome_text,
    delete_welcome_text,
    get_welcome_text_by_id,
    list_welcome_texts,
    update_welcome_text,
)

from ..dependencies import get_db_session, require_api_token
from ..schemas.welcome_texts import (
    WelcomeTextCreateRequest,
    WelcomeTextListResponse,
    WelcomeTextResponse,
    WelcomeTextUpdateRequest,
)


router = APIRouter()


def _serialize(text) -> WelcomeTextResponse:
    return WelcomeTextResponse(
        id=text.id,
        text=text.text_content,
        is_active=text.is_active,
        is_enabled=text.is_enabled,
        created_by=text.created_by,
        created_at=text.created_at,
        updated_at=text.updated_at,
    )


@router.get('', response_model=WelcomeTextListResponse)
async def list_welcome_texts_endpoint(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    include_inactive: bool = Query(True, description='Включать неактивные тексты'),
) -> WelcomeTextListResponse:
    total = await count_welcome_texts(db, include_inactive=include_inactive)
    records = await list_welcome_texts(
        db,
        limit=limit,
        offset=offset,
        include_inactive=include_inactive,
    )

    return WelcomeTextListResponse(
        items=[_serialize(item) for item in records],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post('', response_model=WelcomeTextResponse, status_code=status.HTTP_201_CREATED)
async def create_welcome_text_endpoint(
    payload: WelcomeTextCreateRequest,
    token: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> WelcomeTextResponse:
    created_by = getattr(token, 'id', None)
    record = await create_welcome_text(
        db,
        text_content=payload.text,
        created_by=created_by,
        is_enabled=payload.is_enabled,
        is_active=payload.is_active,
    )

    return _serialize(record)


@router.get('/{welcome_text_id}', response_model=WelcomeTextResponse)
async def get_welcome_text_endpoint(
    welcome_text_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> WelcomeTextResponse:
    record = await get_welcome_text_by_id(db, welcome_text_id)
    if not record:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_WELCOME_TEXTS_NOT_FOUND', 'Welcome text not found'),
        )

    return _serialize(record)


@router.patch('/{welcome_text_id}', response_model=WelcomeTextResponse)
async def update_welcome_text_endpoint(
    welcome_text_id: int,
    payload: WelcomeTextUpdateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> WelcomeTextResponse:
    record = await get_welcome_text_by_id(db, welcome_text_id)
    if not record:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_WELCOME_TEXTS_NOT_FOUND', 'Welcome text not found'),
        )

    update_payload = payload.dict(exclude_unset=True)
    if 'text' in update_payload:
        update_payload['text_content'] = update_payload.pop('text')
    updated = await update_welcome_text(db, record, **update_payload)
    return _serialize(updated)


@router.delete('/{welcome_text_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_welcome_text_endpoint(
    welcome_text_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    record = await get_welcome_text_by_id(db, welcome_text_id)
    if not record:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_WELCOME_TEXTS_NOT_FOUND', 'Welcome text not found'),
        )

    await delete_welcome_text(db, record)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
