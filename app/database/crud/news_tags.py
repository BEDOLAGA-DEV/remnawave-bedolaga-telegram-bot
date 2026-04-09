"""CRUD operations for news tags."""

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import NewsArticle, NewsTag


logger = structlog.get_logger(__name__)


async def get_all_tags(db: AsyncSession) -> list[NewsTag]:
    """Get all news tags ordered by name."""
    result = await db.execute(select(NewsTag).order_by(NewsTag.name))
    return list(result.scalars().all())


async def get_tag_by_id(db: AsyncSession, tag_id: int) -> NewsTag | None:
    """Get a single news tag by primary key."""
    result = await db.execute(select(NewsTag).where(NewsTag.id == tag_id))
    return result.scalar_one_or_none()


async def create_tag(db: AsyncSession, *, name: str, color: str = '#94a3b8') -> NewsTag:
    """Create a new news tag.

    Raises:
        IntegrityError: if a tag with the same name already exists (caller must handle).
    """
    tag = NewsTag(name=name.strip(), color=color)
    db.add(tag)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise
    await db.refresh(tag)
    logger.info('Created news tag', tag_id=tag.id, name=tag.name)
    return tag


async def update_tag(
    db: AsyncSession,
    tag: NewsTag,
    **kwargs: str | None,
) -> NewsTag:
    """Update an existing news tag.

    Supported kwargs: name, color.

    Raises:
        IntegrityError: if the new name conflicts with an existing tag.
    """
    update_data: dict[str, str] = {}
    if 'name' in kwargs and kwargs['name'] is not None:
        update_data['name'] = kwargs['name'].strip()
    if 'color' in kwargs and kwargs['color'] is not None:
        update_data['color'] = kwargs['color']

    if not update_data:
        return tag

    await db.execute(update(NewsTag).where(NewsTag.id == tag.id).values(**update_data))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise
    await db.refresh(tag)
    logger.info('Updated news tag', tag_id=tag.id, updated_fields=list(update_data.keys()))
    return tag


async def delete_tag(db: AsyncSession, tag: NewsTag) -> None:
    """Delete a news tag and clear tag fields from all linked articles."""
    tag_id, tag_name = tag.id, tag.name
    # Clear legacy string field on articles that reference this tag
    await db.execute(update(NewsArticle).where(NewsArticle.tag_id == tag_id).values(tag=None, tag_id=None))
    await db.delete(tag)
    await db.commit()
    logger.info('Deleted news tag', tag_id=tag_id, name=tag_name)
