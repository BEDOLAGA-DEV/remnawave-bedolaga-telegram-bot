"""CRUD operations for news articles."""

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import delete, func, nullslast, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import NewsArticle


logger = structlog.get_logger(__name__)

# Fields that can be set via update_news_article
_ALLOWED_UPDATE_FIELDS: frozenset[str] = frozenset(
    {
        'title',
        'slug',
        'content',
        'excerpt',
        'category',
        'category_color',
        'tag',
        'category_id',
        'tag_id',
        'featured_image_url',
        'is_published',
        'is_featured',
        'published_at',
        'read_time_minutes',
    }
)

# Fields that can be explicitly set to None
_NULLABLE_UPDATE_FIELDS: frozenset[str] = frozenset(
    {
        'excerpt',
        'tag',
        'category_id',
        'tag_id',
        'featured_image_url',
        'published_at',
    }
)


async def create_news_article(
    db: AsyncSession,
    *,
    title: str,
    slug: str,
    content: str = '',
    excerpt: str | None = None,
    category: str = '',
    category_color: str = '#00e5a0',
    tag: str | None = None,
    category_id: int | None = None,
    tag_id: int | None = None,
    featured_image_url: str | None = None,
    is_published: bool = False,
    is_featured: bool = False,
    published_at: datetime | None = None,
    read_time_minutes: int = 1,
    created_by: int | None = None,
) -> NewsArticle:
    """Create a new news article.

    Raises:
        IntegrityError: if slug is not unique (caller must handle).
    """
    # Auto-set published_at when publishing without explicit date
    if is_published and published_at is None:
        published_at = datetime.now(UTC)

    article = NewsArticle(
        title=title,
        slug=slug,
        content=content,
        excerpt=excerpt,
        category=category,
        category_color=category_color,
        tag=tag,
        category_id=category_id,
        tag_id=tag_id,
        featured_image_url=featured_image_url,
        is_published=is_published,
        is_featured=is_featured,
        published_at=published_at,
        read_time_minutes=read_time_minutes,
        created_by=created_by,
    )

    db.add(article)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise
    await db.refresh(article)

    logger.info(
        'Created news article',
        article_id=article.id,
        slug=article.slug,
        is_published=article.is_published,
    )
    return article


async def get_news_article_by_id(db: AsyncSession, article_id: int) -> NewsArticle | None:
    """Get a news article by ID with author, category, and tag relationships."""
    result = await db.execute(
        select(NewsArticle)
        .options(
            selectinload(NewsArticle.author),
            selectinload(NewsArticle.category_obj),
            selectinload(NewsArticle.tag_obj),
        )
        .where(NewsArticle.id == article_id)
    )
    return result.scalar_one_or_none()


async def get_news_article_by_slug(db: AsyncSession, slug: str) -> NewsArticle | None:
    """Get a news article by slug with author, category, and tag relationships."""
    result = await db.execute(
        select(NewsArticle)
        .options(
            selectinload(NewsArticle.author),
            selectinload(NewsArticle.category_obj),
            selectinload(NewsArticle.tag_obj),
        )
        .where(NewsArticle.slug == slug)
    )
    return result.scalar_one_or_none()


async def get_published_news(
    db: AsyncSession,
    *,
    category: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[NewsArticle]:
    """Get published news articles, ordered by published_at descending.

    Does NOT load the author relationship -- list views do not need it.
    """
    stmt = select(NewsArticle).where(NewsArticle.is_published.is_(True))
    if category:
        stmt = stmt.where(NewsArticle.category == category)

    # NULLs last so articles without published_at don't float to the top in DESC
    stmt = stmt.order_by(nullslast(NewsArticle.published_at.desc())).offset(offset).limit(limit)

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_published_news_count(
    db: AsyncSession,
    *,
    category: str | None = None,
) -> int:
    """Get count of published news articles, optionally filtered by category."""
    stmt = select(func.count(NewsArticle.id)).where(NewsArticle.is_published.is_(True))
    if category:
        stmt = stmt.where(NewsArticle.category == category)

    result = await db.execute(stmt)
    return result.scalar_one() or 0


async def get_all_news(
    db: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[NewsArticle]:
    """Get all news articles (admin), ordered by created_at descending."""
    stmt = select(NewsArticle).order_by(NewsArticle.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_all_news_count(db: AsyncSession) -> int:
    """Get total count of all news articles."""
    result = await db.execute(select(func.count(NewsArticle.id)))
    return result.scalar_one() or 0


async def get_news_categories(db: AsyncSession) -> list[str]:
    """Get distinct categories from published articles."""
    result = await db.execute(
        select(NewsArticle.category)
        .where(NewsArticle.is_published.is_(True))
        .where(NewsArticle.category != '')
        .distinct()
        .order_by(NewsArticle.category)
    )
    return list(result.scalars().all())


async def unfeature_all_news(db: AsyncSession) -> None:
    """Remove featured flag from all articles (so only one can be featured).

    Does NOT commit. The caller must commit the session to persist this change.
    This is intentional — the caller should commit both this operation and the
    subsequent feature operation atomically.
    """
    await db.execute(update(NewsArticle).where(NewsArticle.is_featured.is_(True)).values(is_featured=False))


async def update_news_article(
    db: AsyncSession,
    article: NewsArticle,
    **kwargs: Any,
) -> NewsArticle:
    """Update a news article. Only whitelisted fields are applied.

    Raises:
        IntegrityError: if slug conflicts with another article (caller must handle).
    """
    update_data: dict[str, Any] = {}
    for key, value in kwargs.items():
        if key not in _ALLOWED_UPDATE_FIELDS:
            continue
        if value is None and key not in _NULLABLE_UPDATE_FIELDS:
            continue
        update_data[key] = value

    # Auto-set published_at when transitioning to published
    if update_data.get('is_published') and not article.is_published and not update_data.get('published_at'):
        if article.published_at is None:
            update_data['published_at'] = datetime.now(UTC)

    if not update_data:
        return article

    update_data['updated_at'] = datetime.now(UTC)

    await db.execute(update(NewsArticle).where(NewsArticle.id == article.id).values(**update_data))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise
    await db.refresh(article)

    logger.info(
        'Updated news article',
        article_id=article.id,
        slug=article.slug,
        updated_fields=list(update_data.keys()),
    )
    return article


async def delete_news_article(db: AsyncSession, article: NewsArticle) -> None:
    """Delete a news article."""
    # Capture fields before commit expires the ORM instance attributes
    article_id = article.id
    article_slug = article.slug

    await db.execute(delete(NewsArticle).where(NewsArticle.id == article_id))
    await db.commit()

    logger.info('Deleted news article', article_id=article_id, slug=article_slug)


async def increment_views(db: AsyncSession, article_id: int) -> int:
    """Atomically increment the views counter and return the new count.

    Uses UPDATE … RETURNING so the caller can patch the ORM instance directly
    without issuing a second SELECT (db.refresh).
    """
    result = await db.execute(
        update(NewsArticle)
        .where(NewsArticle.id == article_id)
        .values(views_count=NewsArticle.views_count + 1)
        .returning(NewsArticle.views_count)
    )
    await db.commit()
    row = result.fetchone()
    return row[0] if row else 0
