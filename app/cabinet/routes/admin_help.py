"""Admin routes for managing help/FAQ articles in cabinet."""

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.help_article import (
    create_help_article,
    delete_help_article,
    get_all_help_articles,
    get_all_help_articles_count,
    get_help_article_by_id,
    update_help_article,
)
from app.database.models import HelpArticle, User

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.help_article import (
    HelpArticleListItem,
    HelpArticleResponse,
    HelpCreateRequest,
    HelpListResponse,
    HelpToggleResponse,
    HelpUpdateRequest,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/help', tags=['Cabinet Admin Help'])


def _article_to_detail(article: HelpArticle) -> dict[str, Any]:
    """Convert HelpArticle ORM instance to full detail dict."""
    author_name: str | None = None
    if article.author:
        author_name = article.author.first_name or article.author.username or f'#{article.author.id}'

    return {
        'id': article.id,
        'title': article.title,
        'slug': article.slug,
        'content': article.content,
        'excerpt': article.excerpt,
        'category': article.category,
        'category_icon': article.category_icon,
        'category_color': article.category_color,
        'locale': article.locale,
        'display_order': article.display_order,
        'is_published': article.is_published,
        'is_featured': article.is_featured,
        'views_count': article.views_count,
        'helpful_count': article.helpful_count,
        'not_helpful_count': article.not_helpful_count,
        'author_name': author_name,
        'created_at': article.created_at,
        'updated_at': article.updated_at,
    }


@router.get('', response_model=HelpListResponse)
async def list_all_help_articles(
    admin: User = Depends(require_permission('help:read')),
    db: AsyncSession = Depends(get_cabinet_db),
    locale: str | None = Query(None, max_length=10),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> HelpListResponse:
    """Get all help articles (admin view, includes unpublished)."""
    try:
        articles = await get_all_help_articles(db, locale=locale, limit=limit, offset=offset)
        total = await get_all_help_articles_count(db, locale=locale)

        items = [HelpArticleListItem.model_validate(a) for a in articles]
        return HelpListResponse(items=items, total=total)
    except HTTPException:
        raise
    except Exception:
        logger.exception('Failed to list all help articles')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load help articles',
        )


@router.get('/{article_id}', response_model=HelpArticleResponse)
async def get_help_article_detail(
    article_id: int,
    admin: User = Depends(require_permission('help:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> HelpArticleResponse:
    """Get a single help article by ID (admin view)."""
    article = await get_help_article_by_id(db, article_id)
    if not article:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Help article not found',
        )
    return HelpArticleResponse(**_article_to_detail(article))


@router.post('', response_model=HelpArticleResponse, status_code=status.HTTP_201_CREATED)
async def create_help_article_endpoint(
    request: HelpCreateRequest,
    admin: User = Depends(require_permission('help:create')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> HelpArticleResponse:
    """Create a new help article."""
    try:
        article = await create_help_article(
            db,
            title=request.title,
            slug=request.slug or request.title,  # schema guarantees slug is set via validator
            content=request.content,
            excerpt=request.excerpt,
            category=request.category,
            category_icon=request.category_icon,
            category_color=request.category_color,
            locale=request.locale,
            display_order=request.display_order,
            is_published=request.is_published,
            is_featured=request.is_featured,
            created_by=admin.id,
        )
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='A help article with this slug already exists for this locale',
        )
    except Exception:
        logger.exception('Failed to create help article')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to create help article',
        )

    article = await get_help_article_by_id(db, article.id)
    if not article:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to reload help article after creation',
        )
    return HelpArticleResponse(**_article_to_detail(article))


@router.put('/{article_id}', response_model=HelpArticleResponse)
async def update_help_article_endpoint(
    article_id: int,
    request: HelpUpdateRequest,
    admin: User = Depends(require_permission('help:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> HelpArticleResponse:
    """Update an existing help article."""
    article = await get_help_article_by_id(db, article_id)
    if not article:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Help article not found',
        )

    try:
        update_data = request.model_dump(exclude_unset=True)
        article = await update_help_article(db, article, **update_data)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='A help article with this slug already exists for this locale',
        )
    except Exception:
        logger.exception('Failed to update help article', article_id=article_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to update help article',
        )

    article = await get_help_article_by_id(db, article.id)
    if not article:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to reload help article after update',
        )
    return HelpArticleResponse(**_article_to_detail(article))


@router.delete('/{article_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_help_article_endpoint(
    article_id: int,
    admin: User = Depends(require_permission('help:delete')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> None:
    """Delete a help article."""
    article = await get_help_article_by_id(db, article_id)
    if not article:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Help article not found',
        )
    try:
        await delete_help_article(db, article)
    except Exception:
        logger.exception('Failed to delete help article', article_id=article_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to delete help article',
        )


@router.post('/{article_id}/publish', response_model=HelpToggleResponse)
async def toggle_help_publish(
    article_id: int,
    admin: User = Depends(require_permission('help:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> HelpToggleResponse:
    """Toggle the published status of a help article."""
    article = await get_help_article_by_id(db, article_id)
    if not article:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Help article not found',
        )

    new_published = not article.is_published
    try:
        article = await update_help_article(db, article, is_published=new_published)
        return HelpToggleResponse(
            id=article.id,
            is_published=article.is_published,
            is_featured=article.is_featured,
        )
    except Exception:
        logger.exception('Failed to toggle help article publish', article_id=article_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to toggle publish state',
        )


@router.post('/{article_id}/feature', response_model=HelpToggleResponse)
async def toggle_help_feature(
    article_id: int,
    admin: User = Depends(require_permission('help:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> HelpToggleResponse:
    """Toggle the featured flag of a help article."""
    article = await get_help_article_by_id(db, article_id)
    if not article:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Help article not found',
        )

    new_featured = not article.is_featured
    try:
        article = await update_help_article(db, article, is_featured=new_featured)
        return HelpToggleResponse(
            id=article.id,
            is_published=article.is_published,
            is_featured=article.is_featured,
        )
    except Exception:
        logger.exception('Failed to toggle help article feature', article_id=article_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to toggle feature state',
        )
