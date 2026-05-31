from __future__ import annotations

from urllib.parse import urlparse

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PartnerPromo


logger = structlog.get_logger(__name__)


def _is_safe_url(value: str | None) -> bool:
    if not value or not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    return parsed.scheme == 'https' and bool(parsed.netloc)


async def list_active(db: AsyncSession) -> list[PartnerPromo]:
    result = await db.execute(
        select(PartnerPromo)
        .where(PartnerPromo.is_active == True)  # noqa: E712
        .order_by(PartnerPromo.sort_order.asc(), PartnerPromo.id.asc())
    )
    return list(result.scalars().all())


async def list_all(db: AsyncSession) -> list[PartnerPromo]:
    result = await db.execute(
        select(PartnerPromo).order_by(PartnerPromo.sort_order.asc(), PartnerPromo.id.asc())
    )
    return list(result.scalars().all())


async def get(db: AsyncSession, promo_id: int) -> PartnerPromo | None:
    result = await db.execute(select(PartnerPromo).where(PartnerPromo.id == promo_id))
    return result.scalar_one_or_none()


async def create(db: AsyncSession, *, title: dict, url: str, description: dict | None = None,
                 image_url: str | None = None, is_active: bool = True, sort_order: int = 0) -> PartnerPromo:
    if not _is_safe_url(url):
        raise ValueError('url must be https')
    if image_url is not None and image_url != '' and not _is_safe_url(image_url):
        raise ValueError('image_url must be https')
    promo = PartnerPromo(
        title=title or {}, description=description or {}, url=url,
        image_url=image_url or None, is_active=is_active, sort_order=sort_order,
    )
    db.add(promo)
    await db.commit()
    await db.refresh(promo)
    return promo


async def update_promo(db: AsyncSession, promo_id: int, **fields) -> PartnerPromo | None:
    if 'url' in fields and not _is_safe_url(fields['url']):
        raise ValueError('url must be https')
    if fields.get('image_url') and not _is_safe_url(fields['image_url']):
        raise ValueError('image_url must be https')
    promo = await get(db, promo_id)
    if promo is None:
        return None
    for k, v in fields.items():
        if hasattr(promo, k):
            setattr(promo, k, v)
    await db.commit()
    await db.refresh(promo)
    return promo


async def delete(db: AsyncSession, promo_id: int) -> bool:
    promo = await get(db, promo_id)
    if promo is None:
        return False
    await db.delete(promo)
    await db.commit()
    return True


async def increment_click(db: AsyncSession, promo_id: int) -> None:
    """Atomic click++ (no read-modify-write race)."""
    await db.execute(
        update(PartnerPromo)
        .where(PartnerPromo.id == promo_id)
        .values(click_count=PartnerPromo.click_count + 1)
    )
    await db.commit()
