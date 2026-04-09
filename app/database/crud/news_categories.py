"""CRUD operations for news categories."""

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import NewsArticle, NewsCategory


logger = structlog.get_logger(__name__)


async def get_all_categories(db: AsyncSession) -> list[NewsCategory]:
    """Get all news categories ordered by name."""
    result = await db.execute(select(NewsCategory).order_by(NewsCategory.name))
    return list(result.scalars().all())


async def get_category_by_id(db: AsyncSession, category_id: int) -> NewsCategory | None:
    """Get a single news category by primary key."""
    result = await db.execute(select(NewsCategory).where(NewsCategory.id == category_id))
    return result.scalar_one_or_none()


async def create_category(db: AsyncSession, *, name: str, color: str = '#00e5a0') -> NewsCategory:
    """Create a new news category.

    Raises:
        IntegrityError: if a category with the same name already exists (caller must handle).
    """
    category = NewsCategory(name=name.strip(), color=color)
    db.add(category)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise
    await db.refresh(category)
    logger.info('Created news category', category_id=category.id, name=category.name)
    return category


async def update_category(
    db: AsyncSession,
    category: NewsCategory,
    **kwargs: str | None,
) -> NewsCategory:
    """Update an existing news category.

    Supported kwargs: name, color.

    Raises:
        IntegrityError: if the new name conflicts with an existing category.
    """
    update_data: dict[str, str] = {}
    if 'name' in kwargs and kwargs['name'] is not None:
        update_data['name'] = kwargs['name'].strip()
    if 'color' in kwargs and kwargs['color'] is not None:
        update_data['color'] = kwargs['color']

    if not update_data:
        return category

    await db.execute(update(NewsCategory).where(NewsCategory.id == category.id).values(**update_data))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise
    await db.refresh(category)
    logger.info('Updated news category', category_id=category.id, updated_fields=list(update_data.keys()))
    return category


async def delete_category(db: AsyncSession, category: NewsCategory) -> None:
    """Delete a news category and clear category fields from all linked articles."""
    cat_id, cat_name = category.id, category.name
    # Clear legacy string fields on articles that reference this category
    await db.execute(
        update(NewsArticle)
        .where(NewsArticle.category_id == cat_id)
        .values(category='', category_color='#00e5a0', category_id=None)
    )
    await db.delete(category)
    await db.commit()
    logger.info('Deleted news category', category_id=cat_id, name=cat_name)
