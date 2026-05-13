"""CRUD operations for info pages."""

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import InfoPage


logger = structlog.get_logger(__name__)

# Fields that can be set via update_info_page
_ALLOWED_UPDATE_FIELDS: frozenset[str] = frozenset(
    {
        'slug',
        'title',
        'content',
        'page_type',
        'is_active',
        'sort_order',
        'icon',
        'replaces_tab',
    }
)

# Fields that can be explicitly set to None
_NULLABLE_UPDATE_FIELDS: frozenset[str] = frozenset(
    {
        'icon',
        'replaces_tab',
    }
)


async def create_info_page(
    db: AsyncSession,
    *,
    slug: str,
    title: dict[str, str],
    content: dict[str, str],
    page_type: str = 'page',
    is_active: bool = True,
    sort_order: int = 0,
    icon: str | None = None,
    replaces_tab: str | None = None,
) -> InfoPage:
    """Create a new info page.

    Raises:
        IntegrityError: if slug is not unique (caller must handle).
    """
    page = InfoPage(
        slug=slug,
        title=title,
        content=content,
        page_type=page_type,
        is_active=is_active,
        sort_order=sort_order,
        icon=icon,
        replaces_tab=replaces_tab,
    )

    db.add(page)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise
    await db.refresh(page)

    logger.info('Created info page', page_id=page.id, slug=page.slug)
    return page


async def get_info_page_by_id(db: AsyncSession, page_id: int) -> InfoPage | None:
    """Get an info page by ID."""
    result = await db.execute(select(InfoPage).where(InfoPage.id == page_id))
    return result.scalar_one_or_none()


async def get_info_page_by_slug(db: AsyncSession, slug: str) -> InfoPage | None:
    """Get an info page by slug."""
    result = await db.execute(select(InfoPage).where(InfoPage.slug == slug))
    return result.scalar_one_or_none()


async def get_all_info_pages(
    db: AsyncSession,
    *,
    include_inactive: bool = False,
    page_type: str | None = None,
) -> list[InfoPage]:
    """Get all info pages, ordered by sort_order ascending."""
    stmt = select(InfoPage)
    if not include_inactive:
        stmt = stmt.where(InfoPage.is_active.is_(True))
    if page_type is not None:
        stmt = stmt.where(InfoPage.page_type == page_type)

    stmt = stmt.order_by(InfoPage.sort_order.asc(), InfoPage.id.asc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def update_info_page(
    db: AsyncSession,
    page_id: int,
    **kwargs: Any,
) -> InfoPage | None:
    """Update an info page. Only whitelisted fields are applied.

    Raises:
        IntegrityError: if slug conflicts with another page (caller must handle).
    """
    update_data: dict[str, Any] = {}
    for key, value in kwargs.items():
        if key not in _ALLOWED_UPDATE_FIELDS:
            continue
        if value is None and key not in _NULLABLE_UPDATE_FIELDS:
            continue
        update_data[key] = value

    if not update_data:
        return await get_info_page_by_id(db, page_id)

    update_data['updated_at'] = datetime.now(UTC)

    await db.execute(update(InfoPage).where(InfoPage.id == page_id).values(**update_data))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise

    page = await get_info_page_by_id(db, page_id)
    if page:
        logger.info(
            'Updated info page',
            page_id=page_id,
            updated_fields=list(update_data.keys()),
        )
    return page


async def delete_info_page(db: AsyncSession, page_id: int) -> None:
    """Delete an info page."""
    await db.execute(delete(InfoPage).where(InfoPage.id == page_id))
    await db.commit()

    logger.info('Deleted info page', page_id=page_id)


async def get_tab_replacements(db: AsyncSession) -> dict[str, str | None]:
    """Return a mapping of tab name to info page slug for active pages with replaces_tab set.

    Returns dict like ``{'faq': 'my-custom-faq', 'rules': None, 'privacy': None, 'offer': None}``.
    """
    result_map: dict[str, str | None] = {
        'faq': None,
        'rules': None,
        'privacy': None,
        'offer': None,
    }

    stmt = select(InfoPage).where(
        InfoPage.is_active.is_(True),
        InfoPage.replaces_tab.isnot(None),
    )
    result = await db.execute(stmt)
    for page in result.scalars().all():
        if page.replaces_tab in result_map:
            result_map[page.replaces_tab] = page.slug

    return result_map


async def clear_replaces_tab(db: AsyncSession, tab: str, *, exclude_page_id: int | None = None) -> None:
    """Clear replaces_tab for all pages that currently replace the given tab.

    Optionally exclude a specific page (the one being saved).
    """
    stmt = (
        update(InfoPage)
        .where(
            InfoPage.replaces_tab == tab,
        )
        .values(replaces_tab=None, updated_at=datetime.now(UTC))
    )
    if exclude_page_id is not None:
        stmt = stmt.where(InfoPage.id != exclude_page_id)

    await db.execute(stmt)


async def reorder_info_pages(db: AsyncSession, items: list[dict]) -> None:
    """Bulk update sort_order for info pages.

    Each item must have ``id`` and ``sort_order`` attributes.
    """
    for item in items:
        page_id = item.id if hasattr(item, 'id') else item.get('id')
        sort_order = item.sort_order if hasattr(item, 'sort_order') else item.get('sort_order')
        if page_id is None or sort_order is None:
            continue
        await db.execute(
            update(InfoPage).where(InfoPage.id == page_id).values(sort_order=sort_order, updated_at=datetime.now(UTC))
        )

    await db.commit()
    logger.info('Reordered info pages', count=len(items))
