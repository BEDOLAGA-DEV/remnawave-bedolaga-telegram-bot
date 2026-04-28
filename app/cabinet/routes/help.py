"""Public help/FAQ routes for cabinet - user-facing help center section."""

import time
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.help_article import (
    get_help_article_by_slug,
    get_help_categories,
    get_published_help_articles,
    get_published_help_articles_count,
    increment_help_article_views,
    record_helpful_vote,
)
from app.database.models import HelpArticle, User

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.help_article import (
    HelpArticleListItem,
    HelpArticleResponse,
    HelpCategory,
    HelpfulVoteRequest,
    HelpfulVoteResponse,
    HelpListResponse,
)


logger = structlog.get_logger(__name__)

# Slug constraint: alphanumeric, hyphens, underscores, max 500 chars
_SLUG_MAX_LENGTH: int = 500
_SLUG_PATTERN: str = r'^[a-zA-Z0-9_-]+$'

# Allowed locale query values (keep in sync with schemas.help_article)
_ALLOWED_LOCALES: frozenset[str] = frozenset({'ru', 'en', 'fa', 'zh'})

# --- View counter deduplication ---
_VIEW_DEDUP_SECONDS: int = 300
_VIEW_DEDUP_MAX_SIZE: int = 10_000
_view_dedup_cache: dict[tuple[int, int], float] = {}

# --- Helpful-vote deduplication (one vote per user per article per 24h) ---
_VOTE_DEDUP_SECONDS: int = 86_400
_VOTE_DEDUP_MAX_SIZE: int = 10_000
_vote_dedup_cache: dict[tuple[int, int], float] = {}


def _should_count_view(user_id: int, article_id: int) -> bool:
    """Return True if this view should be counted (not a duplicate within TTL)."""
    now = time.monotonic()
    key = (user_id, article_id)
    last_seen = _view_dedup_cache.get(key)
    if last_seen is not None and (now - last_seen) < _VIEW_DEDUP_SECONDS:
        return False
    if len(_view_dedup_cache) >= _VIEW_DEDUP_MAX_SIZE:
        cutoff = now - _VIEW_DEDUP_SECONDS
        stale_keys = [k for k, v in _view_dedup_cache.items() if v < cutoff]
        for k in stale_keys:
            del _view_dedup_cache[k]
    _view_dedup_cache[key] = now
    return True


def _should_accept_vote(user_id: int, article_id: int) -> bool:
    """Return True if this vote should be accepted (not a duplicate within 24h)."""
    now = time.monotonic()
    key = (user_id, article_id)
    last_seen = _vote_dedup_cache.get(key)
    if last_seen is not None and (now - last_seen) < _VOTE_DEDUP_SECONDS:
        return False
    if len(_vote_dedup_cache) >= _VOTE_DEDUP_MAX_SIZE:
        cutoff = now - _VOTE_DEDUP_SECONDS
        stale_keys = [k for k, v in _vote_dedup_cache.items() if v < cutoff]
        for k in stale_keys:
            del _vote_dedup_cache[k]
    _vote_dedup_cache[key] = now
    return True


router = APIRouter(prefix='/help', tags=['Cabinet Help'])


def _article_to_response(article: HelpArticle, *, include_content: bool = True) -> dict[str, Any]:
    """Convert HelpArticle ORM instance to response dict.

    ``author_name`` is only resolved when ``include_content=True`` because the
    author relationship is not eagerly loaded in list queries.
    """
    data: dict[str, Any] = {
        'id': article.id,
        'title': article.title,
        'slug': article.slug,
        'excerpt': article.excerpt,
        'category': article.category,
        'category_icon': article.category_icon,
        'category_color': article.category_color,
        'locale': article.locale,
        'display_order': article.display_order,
        'is_published': article.is_published,
        'is_featured': article.is_featured,
        'views_count': article.views_count,
    }

    if include_content:
        author_name: str | None = None
        if article.author:
            author_name = article.author.first_name or article.author.username or f'#{article.author.id}'
        data['content'] = article.content
        data['helpful_count'] = article.helpful_count
        data['not_helpful_count'] = article.not_helpful_count
        data['author_name'] = author_name
        data['created_at'] = article.created_at
        data['updated_at'] = article.updated_at

    return data


def _normalize_locale(locale: str | None) -> str | None:
    """Normalize a locale query string. Returns None if not set or invalid."""
    if not locale:
        return None
    normalized = locale.lower().strip()
    return normalized if normalized in _ALLOWED_LOCALES else None


# NOTE: /categories MUST be declared before /{slug} to avoid route conflict
@router.get('/categories', response_model=list[HelpCategory])
async def list_help_categories(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    locale: str | None = Query(None, max_length=10),
) -> list[HelpCategory]:
    """Get list of distinct help categories with icon/color metadata."""
    try:
        normalized = _normalize_locale(locale)
        categories = await get_help_categories(db, locale=normalized)
        return [HelpCategory(**c) for c in categories]
    except Exception:
        logger.exception('Failed to get help categories')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load categories',
        )


@router.get('', response_model=HelpListResponse)
async def list_help_articles(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    locale: str | None = Query(None, max_length=10),
    category: str | None = Query(None, max_length=100),
    search: str | None = Query(None, max_length=200),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> HelpListResponse:
    """Get paginated list of published help articles."""
    try:
        normalized_locale = _normalize_locale(locale)

        articles = await get_published_help_articles(
            db,
            locale=normalized_locale,
            category=category,
            search=search,
            limit=limit,
            offset=offset,
        )
        total = await get_published_help_articles_count(
            db,
            locale=normalized_locale,
            category=category,
            search=search,
        )
        categories = await get_help_categories(db, locale=normalized_locale)

        items = [HelpArticleListItem(**_article_to_response(a, include_content=False)) for a in articles]

        return HelpListResponse(
            items=items,
            total=total,
            categories=[HelpCategory(**c) for c in categories],
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception('Failed to list help articles')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load help articles',
        )


@router.get('/{slug}', response_model=HelpArticleResponse)
async def get_help_article(
    slug: str = Path(..., max_length=_SLUG_MAX_LENGTH, pattern=_SLUG_PATTERN),
    locale: str | None = Query(None, max_length=10),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> HelpArticleResponse:
    """Get a single published help article by slug. Increments view count."""
    normalized_locale = _normalize_locale(locale)

    article = await get_help_article_by_slug(db, slug, locale=normalized_locale)
    if not article or not article.is_published:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Help article not found',
        )

    # Build response dict while session attributes are still loaded.
    response_data = _article_to_response(article, include_content=True)

    # Increment views with per-user deduplication (5-min TTL).
    if _should_count_view(user.id, article.id):
        try:
            new_count = await increment_help_article_views(db, article.id)
            response_data['views_count'] = new_count
        except Exception:
            logger.warning('Failed to increment help article views', article_id=article.id)

    return HelpArticleResponse(**response_data)


@router.post('/{slug}/feedback', response_model=HelpfulVoteResponse)
async def submit_help_article_feedback(
    payload: HelpfulVoteRequest,
    slug: str = Path(..., max_length=_SLUG_MAX_LENGTH, pattern=_SLUG_PATTERN),
    locale: str | None = Query(None, max_length=10),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> HelpfulVoteResponse:
    """Submit a helpful / not-helpful vote for an article."""
    normalized_locale = _normalize_locale(locale)

    article = await get_help_article_by_slug(db, slug, locale=normalized_locale)
    if not article or not article.is_published:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Help article not found',
        )

    if not _should_accept_vote(user.id, article.id):
        # Don't increment, but return current counters so the UI can still show state
        return HelpfulVoteResponse(
            id=article.id,
            helpful_count=article.helpful_count,
            not_helpful_count=article.not_helpful_count,
        )

    try:
        helpful_count, not_helpful_count = await record_helpful_vote(
            db, article.id, helpful=payload.helpful
        )
    except Exception:
        logger.exception('Failed to record helpful vote', article_id=article.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to record vote',
        )

    return HelpfulVoteResponse(
        id=article.id,
        helpful_count=helpful_count,
        not_helpful_count=not_helpful_count,
    )
