from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PersonalDataConsent


logger = structlog.get_logger(__name__)


async def get_personal_data_consent(db: AsyncSession, language: str) -> PersonalDataConsent | None:
    result = await db.execute(
        select(PersonalDataConsent).where(PersonalDataConsent.language == language)
    )
    return result.scalar_one_or_none()


async def upsert_personal_data_consent(
    db: AsyncSession,
    language: str,
    content: str,
    *,
    enable_if_new: bool = True,
) -> PersonalDataConsent:
    consent = await get_personal_data_consent(db, language)

    if consent:
        consent.content = content or ''
        consent.updated_at = datetime.now(UTC)
    else:
        consent = PersonalDataConsent(
            language=language,
            content=content or '',
            is_enabled=bool(enable_if_new),
        )
        db.add(consent)

    await db.commit()
    await db.refresh(consent)

    logger.info(
        '✅ Согласие на обработку ПД обновлено для языка',
        language=language,
        consent_id=consent.id,
    )

    return consent


async def set_personal_data_consent_enabled(
    db: AsyncSession,
    language: str,
    enabled: bool,
) -> PersonalDataConsent:
    consent = await get_personal_data_consent(db, language)

    if consent:
        consent.is_enabled = bool(enabled)
        consent.updated_at = datetime.now(UTC)
    else:
        consent = PersonalDataConsent(
            language=language,
            content='',
            is_enabled=bool(enabled),
        )
        db.add(consent)

    await db.commit()
    await db.refresh(consent)

    return consent
