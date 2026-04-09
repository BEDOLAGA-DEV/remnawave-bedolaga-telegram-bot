"""Admin routes for managing news categories."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.news_categories import (
    create_category,
    delete_category,
    get_all_categories,
    get_category_by_id,
    update_category,
)
from app.database.models import User

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.news_categories import NewsCategoryCreate, NewsCategoryResponse, NewsCategoryUpdate


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/news/categories', tags=['Cabinet Admin News Categories'])


@router.get('', response_model=list[NewsCategoryResponse])
async def list_categories(
    admin: User = Depends(require_permission('news:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> list[NewsCategoryResponse]:
    """Get all news categories."""
    categories = await get_all_categories(db)
    return [NewsCategoryResponse.model_validate(c) for c in categories]


@router.post('', response_model=NewsCategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_new_category(
    request: NewsCategoryCreate,
    admin: User = Depends(require_permission('news:create')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> NewsCategoryResponse:
    """Create a new news category."""
    try:
        category = await create_category(db, name=request.name, color=request.color)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Category already exists',
        )
    return NewsCategoryResponse.model_validate(category)


@router.put('/{category_id}', response_model=NewsCategoryResponse)
async def update_existing_category(
    category_id: int,
    request: NewsCategoryUpdate,
    admin: User = Depends(require_permission('news:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> NewsCategoryResponse:
    """Update an existing news category."""
    category = await get_category_by_id(db, category_id)
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Category not found',
        )
    try:
        category = await update_category(db, category, **request.model_dump(exclude_unset=True))
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Category name already exists',
        )
    return NewsCategoryResponse.model_validate(category)


@router.delete('/{category_id}', status_code=status.HTTP_204_NO_CONTENT)
async def remove_category(
    category_id: int,
    admin: User = Depends(require_permission('news:delete')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> None:
    """Delete a news category. Articles using it will have category_id set to NULL."""
    category = await get_category_by_id(db, category_id)
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Category not found',
        )
    await delete_category(db, category)
