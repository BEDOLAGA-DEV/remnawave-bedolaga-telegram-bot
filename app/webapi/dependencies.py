from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.database import AsyncSessionLocal
from app.database.models import WebApiToken
from app.localization.texts import get_texts
from app.services.web_api_token_service import web_api_token_service


api_key_header_scheme = APIKeyHeader(name='X-API-Key', auto_error=False)



async def get_db_session() -> AsyncGenerator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def require_api_token(
    request: Request,
    api_key_header: str | None = Security(api_key_header_scheme),
    db: AsyncSession = Depends(get_db_session),
) -> WebApiToken:
    texts = get_texts('ru')
    api_key = api_key_header

    if not api_key:
        authorization = request.headers.get('Authorization')
        if authorization:
            scheme, _, credentials = authorization.partition(' ')
            if scheme.lower() == 'bearer' and credentials:
                api_key = credentials

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=texts.t('WEBAPI_DEPENDENCIES_MISSING_API_KEY', 'Missing API key'),
        )

    token = await web_api_token_service.authenticate(
        db,
        api_key,
        remote_ip=request.client.host if request.client else None,
    )

    if not token:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=texts.t(
                'WEBAPI_DEPENDENCIES_INVALID_OR_EXPIRED_API_KEY',
                'Invalid or expired API key',
            ),
        )

    await db.commit()
    return token
