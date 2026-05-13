"""Public info page routes for cabinet."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.info_pages import get_all_info_pages, get_info_page_by_slug, get_tab_replacements

from ..dependencies import get_cabinet_db
from ..schemas.info_pages import InfoPageListItem, InfoPageResponse


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/info-pages', tags=['Cabinet Info Pages'])


@router.get('', response_model=list[InfoPageListItem])
async def list_active_info_pages(
    page_type: str | None = Query(None, pattern=r'^(page|faq)$'),
    db: AsyncSession = Depends(get_cabinet_db),
) -> list[InfoPageListItem]:
    """Get all active info pages (public, no auth required)."""
    try:
        pages = await get_all_info_pages(db, include_inactive=False, page_type=page_type)
        return [InfoPageListItem.model_validate(p) for p in pages]
    except Exception:
        logger.exception('Failed to list active info pages')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load info pages',
        )


@router.get('/tab-replacements')
async def get_info_page_tab_replacements(
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, str | None]:
    """Get tab replacement mapping (public, no auth required).

    Returns a dict mapping each replaceable tab to the info page slug that replaces it,
    or null if no replacement is set: ``{faq: slug_or_null, ...}``.
    """
    try:
        return await get_tab_replacements(db)
    except Exception:
        logger.exception('Failed to get tab replacements')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load tab replacements',
        )


@router.get('/{slug}', response_model=InfoPageResponse)
async def get_info_page_by_slug_public(
    slug: str = Path(..., max_length=200, pattern=r'^[a-z0-9\-]+$'),
    db: AsyncSession = Depends(get_cabinet_db),
) -> InfoPageResponse:
    """Get a single info page by slug (public, no auth required)."""
    page = await get_info_page_by_slug(db, slug)

    if not page or not page.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Info page not found',
        )

    return InfoPageResponse.model_validate(page)
