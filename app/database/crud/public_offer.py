from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PublicOffer


logger = structlog.get_logger(__name__)


async def get_public_offer(db: AsyncSession, language: str) -> PublicOffer | None:
    result = await db.execute(select(PublicOffer).where(PublicOffer.language == language))
    return result.scalar_one_or_none()


async def upsert_public_offer(
    db: AsyncSession,
    language: str,
    content: str,
    *,
    enable_if_new: bool = True,
    is_enabled: bool | None = None,
) -> PublicOffer:
    offer = await get_public_offer(db, language)

    if offer:
        offer.content = content or ''
        if is_enabled is not None:
            offer.is_enabled = bool(is_enabled)
        offer.updated_at = datetime.now(UTC)
    else:
        offer = PublicOffer(
            language=language,
            content=content or '',
            is_enabled=bool(enable_if_new) if is_enabled is None else bool(is_enabled),
        )
        db.add(offer)

    await db.commit()
    await db.refresh(offer)

    logger.info('✅ Публичная оферта обновлена', language=language, offer_id=offer.id)

    return offer


async def set_public_offer_enabled(
    db: AsyncSession,
    language: str,
    enabled: bool,
) -> PublicOffer:
    offer = await get_public_offer(db, language)

    if offer:
        offer.is_enabled = bool(enabled)
        offer.updated_at = datetime.now(UTC)
    else:
        offer = PublicOffer(
            language=language,
            content='',
            is_enabled=bool(enabled),
        )
        db.add(offer)

    await db.commit()
    await db.refresh(offer)

    logger.info(
        '✅ Статус публичной оферты для языка %s обновлен: %s',
        language,
        'enabled' if offer.is_enabled else 'disabled',
    )

    return offer
