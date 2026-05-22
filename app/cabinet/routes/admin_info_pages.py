"""Admin routes for managing info pages in cabinet."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.info_pages import (
    clear_replaces_tab,
    create_info_page,
    delete_info_page,
    get_all_info_pages,
    get_info_page_by_id,
    reorder_info_pages,
    update_info_page,
)
from app.database.models import User

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.info_pages import (
    InfoPageCreateRequest,
    InfoPageListItem,
    InfoPageResponse,
    InfoPageUpdateRequest,
    ReorderRequest,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/info-pages', tags=['Cabinet Admin Info Pages'])


@router.get('', response_model=list[InfoPageListItem])
async def list_all_info_pages(
    page_type: str | None = Query(None, pattern=r'^(page|faq)$'),
    admin: User = Depends(require_permission('info_pages:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> list[InfoPageListItem]:
    """Get all info pages (admin view, includes inactive)."""
    try:
        pages = await get_all_info_pages(db, include_inactive=True, page_type=page_type)
        return [InfoPageListItem.model_validate(p) for p in pages]
    except HTTPException:
        raise
    except Exception:
        logger.exception('Failed to list info pages')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load info pages',
        )


@router.get('/{page_id}', response_model=InfoPageResponse)
async def get_info_page_detail(
    page_id: int,
    admin: User = Depends(require_permission('info_pages:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> InfoPageResponse:
    """Get a single info page by ID (admin view)."""
    page = await get_info_page_by_id(db, page_id)
    if not page:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Info page not found',
        )
    return InfoPageResponse.model_validate(page)


@router.post('', response_model=InfoPageResponse, status_code=status.HTTP_201_CREATED)
async def create_page(
    request: InfoPageCreateRequest,
    admin: User = Depends(require_permission('info_pages:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> InfoPageResponse:
    """Create a new info page."""
    try:
        if request.replaces_tab:
            await clear_replaces_tab(db, request.replaces_tab)

        page = await create_info_page(
            db,
            slug=request.slug,
            title=request.title,
            content=request.content,
            page_type=request.page_type,
            is_active=request.is_active,
            sort_order=request.sort_order,
            icon=request.icon,
            replaces_tab=request.replaces_tab,
        )
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='An info page with this slug already exists',
        )
    except Exception:
        logger.exception('Failed to create info page')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to create info page',
        )

    return InfoPageResponse.model_validate(page)


@router.put('/{page_id}', response_model=InfoPageResponse)
async def update_page(
    page_id: int,
    request: InfoPageUpdateRequest,
    admin: User = Depends(require_permission('info_pages:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> InfoPageResponse:
    """Update an existing info page."""
    existing = await get_info_page_by_id(db, page_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Info page not found',
        )

    try:
        update_data = request.model_dump(exclude_unset=True)

        replaces_tab = update_data.get('replaces_tab')
        if replaces_tab is not None:
            await clear_replaces_tab(db, replaces_tab, exclude_page_id=page_id)

        page = await update_info_page(db, page_id, **update_data)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='An info page with this slug already exists',
        )
    except Exception:
        logger.exception('Failed to update info page', page_id=page_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to update info page',
        )

    if not page:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Info page not found after update',
        )
    return InfoPageResponse.model_validate(page)


@router.delete('/{page_id}', status_code=status.HTTP_204_NO_CONTENT)
async def remove_page(
    page_id: int,
    admin: User = Depends(require_permission('info_pages:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> None:
    """Delete an info page."""
    existing = await get_info_page_by_id(db, page_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Info page not found',
        )

    try:
        await delete_info_page(db, page_id)
    except Exception:
        logger.exception('Failed to delete info page', page_id=page_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to delete info page',
        )


@router.post('/reorder', status_code=status.HTTP_204_NO_CONTENT)
async def reorder_pages(
    request: ReorderRequest,
    admin: User = Depends(require_permission('info_pages:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> None:
    """Bulk update sort_order for info pages."""
    try:
        await reorder_info_pages(db, request.items)
    except Exception:
        logger.exception('Failed to reorder info pages')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to reorder info pages',
        )


@router.post('/{page_id}/toggle-active', response_model=InfoPageResponse)
async def toggle_active(
    page_id: int,
    admin: User = Depends(require_permission('info_pages:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> InfoPageResponse:
    """Toggle the active status of an info page."""
    existing = await get_info_page_by_id(db, page_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Info page not found',
        )

    try:
        page = await update_info_page(db, page_id, is_active=not existing.is_active)
    except Exception:
        logger.exception('Failed to toggle info page active status', page_id=page_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to toggle active status',
        )

    if not page:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Info page not found after toggle',
        )
    return InfoPageResponse.model_validate(page)
