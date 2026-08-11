from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import RecurrentPayments


logger = structlog.get_logger(__name__)


async def get_recurrent_payments(db: AsyncSession, language: str) -> RecurrentPayments | None:
    result = await db.execute(select(RecurrentPayments).where(RecurrentPayments.language == language))
    return result.scalar_one_or_none()


async def upsert_recurrent_payments(
    db: AsyncSession,
    language: str,
    content: str,
    *,
    enable_if_new: bool = True,
    is_enabled: bool | None = None,
) -> RecurrentPayments:
    document = await get_recurrent_payments(db, language)

    if document:
        document.content = content or ''
        if is_enabled is not None:
            document.is_enabled = bool(is_enabled)
        document.updated_at = datetime.now(UTC)
    else:
        document = RecurrentPayments(
            language=language,
            content=content or '',
            is_enabled=bool(enable_if_new) if is_enabled is None else bool(is_enabled),
        )
        db.add(document)

    await db.commit()
    await db.refresh(document)

    logger.info('✅ Документ о рекуррентных платежах обновлён', language=language, document_id=document.id)

    return document


async def set_recurrent_payments_enabled(
    db: AsyncSession,
    language: str,
    enabled: bool,
) -> RecurrentPayments:
    document = await get_recurrent_payments(db, language)

    if document:
        document.is_enabled = bool(enabled)
        document.updated_at = datetime.now(UTC)
    else:
        document = RecurrentPayments(
            language=language,
            content='',
            is_enabled=bool(enabled),
        )
        db.add(document)

    await db.commit()
    await db.refresh(document)

    logger.info(
        '✅ Статус документа о рекуррентных платежах для языка %s обновлен: %s',
        language,
        'enabled' if document.is_enabled else 'disabled',
    )

    return document
