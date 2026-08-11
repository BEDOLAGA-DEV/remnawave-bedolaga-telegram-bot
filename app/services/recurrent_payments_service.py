import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.recurrent_payments import (
    get_recurrent_payments,
    set_recurrent_payments_enabled,
    upsert_recurrent_payments,
)
from app.database.models import RecurrentPayments


logger = structlog.get_logger(__name__)


class RecurrentPaymentsService:
    """Helpers for managing the recurring-payments legal document and its visibility."""

    @staticmethod
    def _normalize_language(language: str) -> str:
        base_language = language or settings.DEFAULT_LANGUAGE or 'ru'
        return base_language.split('-')[0].lower()

    @staticmethod
    def normalize_language(language: str) -> str:
        return RecurrentPaymentsService._normalize_language(language)

    @classmethod
    async def get_document(
        cls,
        db: AsyncSession,
        language: str,
        *,
        fallback: bool = False,
    ) -> RecurrentPayments | None:
        lang = cls._normalize_language(language)
        document = await get_recurrent_payments(db, lang)

        if document or not fallback:
            return document

        default_lang = cls._normalize_language(settings.DEFAULT_LANGUAGE)
        if lang != default_lang:
            return await get_recurrent_payments(db, default_lang)

        return document

    @classmethod
    async def get_active_document(
        cls,
        db: AsyncSession,
        language: str,
    ) -> RecurrentPayments | None:
        lang = cls._normalize_language(language)
        document = await get_recurrent_payments(db, lang)

        if document:
            if document.is_enabled and document.content.strip():
                return document

            if not document.is_enabled:
                return None

        default_lang = cls._normalize_language(settings.DEFAULT_LANGUAGE)
        if lang != default_lang:
            fallback_document = await get_recurrent_payments(db, default_lang)
            if fallback_document and fallback_document.is_enabled and fallback_document.content.strip():
                return fallback_document

        return None

    @classmethod
    async def is_enabled(cls, db: AsyncSession, language: str) -> bool:
        document = await cls.get_active_document(db, language)
        return document is not None

    @classmethod
    async def save_document(
        cls,
        db: AsyncSession,
        language: str,
        content: str,
    ) -> RecurrentPayments:
        lang = cls._normalize_language(language)
        document = await upsert_recurrent_payments(db, lang, content, enable_if_new=True)
        logger.info('✅ Документ о рекуррентных платежах обновлён для языка', lang=lang)
        return document

    @classmethod
    async def set_enabled(
        cls,
        db: AsyncSession,
        language: str,
        enabled: bool,
    ) -> RecurrentPayments:
        lang = cls._normalize_language(language)
        return await set_recurrent_payments_enabled(db, lang, enabled)

    @classmethod
    async def toggle_enabled(
        cls,
        db: AsyncSession,
        language: str,
    ) -> RecurrentPayments:
        lang = cls._normalize_language(language)
        document = await get_recurrent_payments(db, lang)
        new_status = not document.is_enabled if document else True
        return await set_recurrent_payments_enabled(db, lang, new_status)
