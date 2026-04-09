"""Admin routes for managing news tags."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.news_tags import (
    create_tag,
    delete_tag,
    get_all_tags,
    get_tag_by_id,
    update_tag,
)
from app.database.models import User

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.news_tags import NewsTagCreate, NewsTagResponse, NewsTagUpdate


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/news/tags', tags=['Cabinet Admin News Tags'])


@router.get('', response_model=list[NewsTagResponse])
async def list_tags(
    admin: User = Depends(require_permission('news:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> list[NewsTagResponse]:
    """Get all news tags."""
    tags = await get_all_tags(db)
    return [NewsTagResponse.model_validate(t) for t in tags]


@router.post('', response_model=NewsTagResponse, status_code=status.HTTP_201_CREATED)
async def create_new_tag(
    request: NewsTagCreate,
    admin: User = Depends(require_permission('news:create')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> NewsTagResponse:
    """Create a new news tag."""
    try:
        tag = await create_tag(db, name=request.name, color=request.color)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Tag already exists',
        )
    return NewsTagResponse.model_validate(tag)


@router.put('/{tag_id}', response_model=NewsTagResponse)
async def update_existing_tag(
    tag_id: int,
    request: NewsTagUpdate,
    admin: User = Depends(require_permission('news:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> NewsTagResponse:
    """Update an existing news tag."""
    tag = await get_tag_by_id(db, tag_id)
    if not tag:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Tag not found',
        )
    try:
        tag = await update_tag(db, tag, **request.model_dump(exclude_unset=True))
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Tag name already exists',
        )
    return NewsTagResponse.model_validate(tag)


@router.delete('/{tag_id}', status_code=status.HTTP_204_NO_CONTENT)
async def remove_tag(
    tag_id: int,
    admin: User = Depends(require_permission('news:delete')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> None:
    """Delete a news tag. Articles using it will have tag_id set to NULL."""
    tag = await get_tag_by_id(db, tag_id)
    if not tag:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Tag not found',
        )
    await delete_tag(db, tag)
