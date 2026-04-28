"""CRUD operations for help/FAQ articles."""

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import HelpArticle


logger = structlog.get_logger(__name__)

# Fields that can be set via update_help_article
_ALLOWED_UPDATE_FIELDS: frozenset[str] = frozenset(
    {
        'title',
        'slug',
        'content',
        'excerpt',
        'category',
        'category_icon',
        'category_color',
        'locale',
        'display_order',
        'is_published',
        'is_featured',
    }
)

# Fields that can be explicitly set to None
_NULLABLE_UPDATE_FIELDS: frozenset[str] = frozenset(
    {
        'excerpt',
        'category_icon',
    }
)


async def create_help_article(
    db: AsyncSession,
    *,
    title: str,
    slug: str,
    content: str = '',
    excerpt: str | None = None,
    category: str = 'general',
    category_icon: str | None = None,
    category_color: str = '#00e5a0',
    locale: str = 'ru',
    display_order: int = 0,
    is_published: bool = False,
    is_featured: bool = False,
    created_by: int | None = None,
) -> HelpArticle:
    """Create a new help article.

    Raises:
        IntegrityError: if (locale, slug) is not unique (caller must handle).
    """
    article = HelpArticle(
        title=title,
        slug=slug,
        content=content,
        excerpt=excerpt,
        category=category,
        category_icon=category_icon,
        category_color=category_color,
        locale=locale,
        display_order=display_order,
        is_published=is_published,
        is_featured=is_featured,
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
        'Created help article',
        article_id=article.id,
        slug=article.slug,
        locale=article.locale,
        is_published=article.is_published,
    )
    return article


async def get_help_article_by_id(db: AsyncSession, article_id: int) -> HelpArticle | None:
    """Get a help article by ID with author eagerly loaded."""
    result = await db.execute(
        select(HelpArticle)
        .options(selectinload(HelpArticle.author))
        .where(HelpArticle.id == article_id)
    )
    return result.scalar_one_or_none()


async def get_help_article_by_slug(
    db: AsyncSession,
    slug: str,
    *,
    locale: str | None = None,
) -> HelpArticle | None:
    """Get a help article by slug, optionally scoped to a locale.

    If ``locale`` is provided, returns the article matching exact (locale, slug).
    Otherwise returns the first article matching the slug (useful for legacy
    links where locale is not part of the URL).
    """
    stmt = select(HelpArticle).options(selectinload(HelpArticle.author)).where(HelpArticle.slug == slug)
    if locale:
        stmt = stmt.where(HelpArticle.locale == locale)
    stmt = stmt.order_by(HelpArticle.id.asc()).limit(1)

    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_published_help_articles(
    db: AsyncSession,
    *,
    locale: str | None = None,
    category: str | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[HelpArticle]:
    """Get published help articles, ordered by display_order then title.

    Does NOT load the author relationship -- list views do not need it.
    """
    stmt = select(HelpArticle).where(HelpArticle.is_published.is_(True))

    if locale:
        stmt = stmt.where(HelpArticle.locale == locale)
    if category:
        stmt = stmt.where(HelpArticle.category == category)
    if search:
        # Case-insensitive match across title, excerpt, content
        pattern = f'%{search.strip()}%'
        stmt = stmt.where(
            or_(
                HelpArticle.title.ilike(pattern),
                HelpArticle.excerpt.ilike(pattern),
                HelpArticle.content.ilike(pattern),
            )
        )

    stmt = (
        stmt.order_by(
            HelpArticle.category.asc(),
            HelpArticle.display_order.asc(),
            HelpArticle.title.asc(),
        )
        .offset(offset)
        .limit(limit)
    )

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_published_help_articles_count(
    db: AsyncSession,
    *,
    locale: str | None = None,
    category: str | None = None,
    search: str | None = None,
) -> int:
    """Get count of published help articles with optional filters."""
    stmt = select(func.count(HelpArticle.id)).where(HelpArticle.is_published.is_(True))

    if locale:
        stmt = stmt.where(HelpArticle.locale == locale)
    if category:
        stmt = stmt.where(HelpArticle.category == category)
    if search:
        pattern = f'%{search.strip()}%'
        stmt = stmt.where(
            or_(
                HelpArticle.title.ilike(pattern),
                HelpArticle.excerpt.ilike(pattern),
                HelpArticle.content.ilike(pattern),
            )
        )

    result = await db.execute(stmt)
    return result.scalar_one() or 0


async def get_help_categories(
    db: AsyncSession,
    *,
    locale: str | None = None,
) -> list[dict[str, Any]]:
    """Get distinct categories from published articles with icon/color metadata.

    Returns a list of dicts with `name`, `icon`, `color`, and `count` fields,
    sorted by category name. When multiple articles share a category, the icon
    and color of the article with the smallest display_order are used.
    """
    stmt = (
        select(
            HelpArticle.category,
            HelpArticle.category_icon,
            HelpArticle.category_color,
            func.count(HelpArticle.id).label('count'),
            func.min(HelpArticle.display_order).label('min_order'),
        )
        .where(HelpArticle.is_published.is_(True))
        .where(HelpArticle.category != '')
        .group_by(HelpArticle.category, HelpArticle.category_icon, HelpArticle.category_color)
        .order_by(HelpArticle.category.asc())
    )
    if locale:
        stmt = stmt.where(HelpArticle.locale == locale)

    result = await db.execute(stmt)
    rows = result.all()

    # Collapse duplicates across icon/color variants by picking the one with the
    # smallest min_order, then summing counts from the rest.
    by_name: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = row.category
        existing = by_name.get(name)
        if existing is None or row.min_order < existing['_min_order']:
            by_name[name] = {
                'name': name,
                'icon': row.category_icon,
                'color': row.category_color,
                'count': row.count,
                '_min_order': row.min_order,
            }
        else:
            existing['count'] += row.count

    result_list: list[dict[str, Any]] = []
    for name in sorted(by_name.keys()):
        entry = by_name[name]
        entry.pop('_min_order', None)
        result_list.append(entry)
    return result_list


async def get_all_help_articles(
    db: AsyncSession,
    *,
    locale: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[HelpArticle]:
    """Get all help articles (admin), ordered by created_at descending."""
    stmt = select(HelpArticle)
    if locale:
        stmt = stmt.where(HelpArticle.locale == locale)
    stmt = stmt.order_by(HelpArticle.created_at.desc()).offset(offset).limit(limit)

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_all_help_articles_count(
    db: AsyncSession,
    *,
    locale: str | None = None,
) -> int:
    """Get total count of all help articles (admin)."""
    stmt = select(func.count(HelpArticle.id))
    if locale:
        stmt = stmt.where(HelpArticle.locale == locale)
    result = await db.execute(stmt)
    return result.scalar_one() or 0


async def update_help_article(
    db: AsyncSession,
    article: HelpArticle,
    **kwargs: Any,
) -> HelpArticle:
    """Update a help article. Only whitelisted fields are applied.

    Raises:
        IntegrityError: if (locale, slug) conflicts (caller must handle).
    """
    update_data: dict[str, Any] = {}
    for key, value in kwargs.items():
        if key not in _ALLOWED_UPDATE_FIELDS:
            continue
        if value is None and key not in _NULLABLE_UPDATE_FIELDS:
            continue
        update_data[key] = value

    if not update_data:
        return article

    update_data['updated_at'] = datetime.now(UTC)

    await db.execute(update(HelpArticle).where(HelpArticle.id == article.id).values(**update_data))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise
    await db.refresh(article)

    logger.info(
        'Updated help article',
        article_id=article.id,
        slug=article.slug,
        updated_fields=list(update_data.keys()),
    )
    return article


async def delete_help_article(db: AsyncSession, article: HelpArticle) -> None:
    """Delete a help article."""
    article_id = article.id
    article_slug = article.slug

    await db.execute(delete(HelpArticle).where(HelpArticle.id == article_id))
    await db.commit()

    logger.info('Deleted help article', article_id=article_id, slug=article_slug)


async def increment_help_article_views(db: AsyncSession, article_id: int) -> int:
    """Atomically increment views and return the new count."""
    result = await db.execute(
        update(HelpArticle)
        .where(HelpArticle.id == article_id)
        .values(views_count=HelpArticle.views_count + 1)
        .returning(HelpArticle.views_count)
    )
    await db.commit()
    row = result.fetchone()
    return row[0] if row else 0


async def record_helpful_vote(
    db: AsyncSession,
    article_id: int,
    *,
    helpful: bool,
) -> tuple[int, int]:
    """Atomically increment helpful/not_helpful counter and return new (helpful, not_helpful) pair."""
    if helpful:
        stmt = (
            update(HelpArticle)
            .where(HelpArticle.id == article_id)
            .values(helpful_count=HelpArticle.helpful_count + 1)
            .returning(HelpArticle.helpful_count, HelpArticle.not_helpful_count)
        )
    else:
        stmt = (
            update(HelpArticle)
            .where(HelpArticle.id == article_id)
            .values(not_helpful_count=HelpArticle.not_helpful_count + 1)
            .returning(HelpArticle.helpful_count, HelpArticle.not_helpful_count)
        )

    result = await db.execute(stmt)
    await db.commit()
    row = result.fetchone()
    return (row[0], row[1]) if row else (0, 0)
