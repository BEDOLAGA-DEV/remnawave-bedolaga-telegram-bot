"""Public news routes for cabinet - user-facing news/blog section."""

import time
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.news import (
    get_news_article_by_slug,
    get_news_categories,
    get_published_news,
    get_published_news_count,
    increment_views,
)
from app.database.models import NewsArticle, User

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.news import (
    NewsArticleListItem,
    NewsArticleResponse,
    NewsListResponse,
)


logger = structlog.get_logger(__name__)

# Slug constraint: alphanumeric, hyphens, underscores, max 500 chars
_SLUG_MAX_LENGTH: int = 500
_SLUG_PATTERN: str = r'^[a-zA-Z0-9_-]+$'

# --- View counter deduplication ---
# In-memory TTL cache to prevent a single user from inflating view counts.
# Key: (user_id, article_id), Value: timestamp of last counted view.
# Views from the same user on the same article within _VIEW_DEDUP_SECONDS are ignored.
_VIEW_DEDUP_SECONDS: int = 300  # 5 minutes
_VIEW_DEDUP_MAX_SIZE: int = 10_000  # max entries before eviction
_view_dedup_cache: dict[tuple[int, int], float] = {}


def _should_count_view(user_id: int, article_id: int) -> bool:
    """Return True if this view should be counted (not a duplicate within TTL)."""
    now = time.monotonic()
    key = (user_id, article_id)
    last_seen = _view_dedup_cache.get(key)

    if last_seen is not None and (now - last_seen) < _VIEW_DEDUP_SECONDS:
        return False

    # Evict stale entries if cache grows too large
    if len(_view_dedup_cache) >= _VIEW_DEDUP_MAX_SIZE:
        cutoff = now - _VIEW_DEDUP_SECONDS
        stale_keys = [k for k, v in _view_dedup_cache.items() if v < cutoff]
        for k in stale_keys:
            del _view_dedup_cache[k]

    _view_dedup_cache[key] = now
    return True


router = APIRouter(prefix='/news', tags=['Cabinet News'])


def _article_to_response(article: NewsArticle, *, include_content: bool = True) -> dict[str, Any]:
    """Convert NewsArticle ORM instance to response dict.

    ``author_name`` is only resolved when ``include_content=True`` (single-article
    detail view) because the author relationship is not eagerly loaded for list
    queries -- accessing it there would trigger a lazy-load or raise
    ``MissingGreenlet`` in async context.
    """
    data: dict[str, Any] = {
        'id': article.id,
        'title': article.title,
        'slug': article.slug,
        'excerpt': article.excerpt,
        'category': article.category,
        'category_color': article.category_color,
        'tag': article.tag,
        'featured_image_url': article.featured_image_url,
        'is_published': article.is_published,
        'is_featured': article.is_featured,
        'published_at': article.published_at,
        'read_time_minutes': article.read_time_minutes,
        'views_count': article.views_count,
    }

    if include_content:
        author_name: str | None = None
        if article.author:
            author_name = article.author.first_name or article.author.username or f'#{article.author.id}'
        data['content'] = article.content
        data['author_name'] = author_name
        data['created_at'] = article.created_at
        data['updated_at'] = article.updated_at

    return data


# NOTE: /categories MUST be declared before /{slug} to avoid route conflict
@router.get('/categories', response_model=list[str])
async def list_categories(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> list[str]:
    """Get list of distinct news categories."""
    try:
        return await get_news_categories(db)
    except Exception:
        logger.exception('Failed to get news categories')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load categories',
        )


@router.get('', response_model=NewsListResponse)
async def list_published_news(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    category: str | None = Query(None, max_length=100),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> NewsListResponse:
    """Get paginated list of published news articles.

    SQLAlchemy AsyncSession does not support concurrent operations, so
    queries run sequentially.
    """
    try:
        articles = await get_published_news(db, category=category, limit=limit, offset=offset)
        total = await get_published_news_count(db, category=category)
        categories = await get_news_categories(db)

        items = [NewsArticleListItem(**_article_to_response(a, include_content=False)) for a in articles]

        return NewsListResponse(items=items, total=total, categories=categories)
    except HTTPException:
        raise
    except Exception:
        logger.exception('Failed to list published news')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load news',
        )


@router.get('/{slug}', response_model=NewsArticleResponse)
async def get_article_by_slug(
    slug: str = Path(..., max_length=_SLUG_MAX_LENGTH, pattern=_SLUG_PATTERN),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> NewsArticleResponse:
    """Get a single published news article by slug. Increments view count."""
    article = await get_news_article_by_slug(db, slug)

    if not article or not article.is_published:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Article not found',
        )

    # Build response dict while session attributes are still loaded.
    # increment_views() calls db.commit() which expires all ORM attributes;
    # accessing them afterwards triggers lazy-load → MissingGreenlet in async.
    response_data = _article_to_response(article, include_content=True)

    # Increment views with per-user deduplication (5-min TTL).
    if _should_count_view(user.id, article.id):
        try:
            new_count = await increment_views(db, article.id)
            response_data['views_count'] = new_count
        except Exception:
            logger.warning('Failed to increment views', article_id=article.id)

    return NewsArticleResponse(**response_data)
