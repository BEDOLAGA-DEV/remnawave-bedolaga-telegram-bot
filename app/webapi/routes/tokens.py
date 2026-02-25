from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, Security, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.localization.texts import get_texts
from app.database.crud.web_api_token import (
    delete_token,
    get_token_by_id,
    list_tokens,
)
from app.database.models import WebApiToken
from app.services.web_api_token_service import web_api_token_service

from ..dependencies import get_db_session, require_api_token
from ..schemas.tokens import TokenCreateRequest, TokenCreateResponse, TokenResponse


router = APIRouter()


def _serialize(token: WebApiToken) -> TokenResponse:
    return TokenResponse(
        id=token.id,
        name=token.name,
        prefix=token.token_prefix,
        description=token.description,
        is_active=token.is_active,
        created_at=token.created_at,
        updated_at=token.updated_at,
        expires_at=token.expires_at,
        last_used_at=token.last_used_at,
        last_used_ip=token.last_used_ip,
        created_by=token.created_by,
    )


@router.get('', response_model=list[TokenResponse])
async def get_tokens(
    _: WebApiToken = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> list[TokenResponse]:
    tokens = await list_tokens(db, include_inactive=True)
    return [_serialize(token) for token in tokens]


@router.post('', response_model=TokenCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_token(
    payload: TokenCreateRequest,
    actor: WebApiToken = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> TokenCreateResponse:
    token_value, token = await web_api_token_service.create_token(
        db,
        name=payload.name.strip(),
        description=payload.description,
        expires_at=payload.expires_at,
        created_by=actor.name,
    )
    await db.commit()

    base = _serialize(token).model_dump()
    base['token'] = token_value
    return TokenCreateResponse(**base)


@router.post('/{token_id}/revoke', response_model=TokenResponse)
async def revoke_token(
    token_id: int,
    _: WebApiToken = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> TokenResponse:
    token = await get_token_by_id(db, token_id)
    if not token:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_TOKENS_NOT_FOUND', 'Token not found'),
        )

    await web_api_token_service.revoke_token(db, token)
    await db.commit()
    return _serialize(token)


@router.post('/{token_id}/activate', response_model=TokenResponse)
async def activate_token(
    token_id: int,
    _: WebApiToken = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> TokenResponse:
    token = await get_token_by_id(db, token_id)
    if not token:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_TOKENS_NOT_FOUND', 'Token not found'),
        )

    await web_api_token_service.activate_token(db, token)
    await db.commit()
    return _serialize(token)


@router.delete('/{token_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_token_endpoint(
    token_id: int,
    _: WebApiToken = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    token = await get_token_by_id(db, token_id)
    if not token:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_TOKENS_NOT_FOUND', 'Token not found'),
        )

    await delete_token(db, token)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
