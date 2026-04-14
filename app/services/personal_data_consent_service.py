import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.personal_data_consent import (
    get_personal_data_consent,
    set_personal_data_consent_enabled,
    upsert_personal_data_consent,
)
from app.database.models import PersonalDataConsent


logger = structlog.get_logger(__name__)


class PersonalDataConsentService:
    """Helpers for managing the personal data consent text and visibility."""

    @staticmethod
    def _normalize_language(language: str) -> str:
        base_language = language or settings.DEFAULT_LANGUAGE or 'ru'
        return base_language.split('-')[0].lower()

    @staticmethod
    def normalize_language(language: str) -> str:
        return PersonalDataConsentService._normalize_language(language)

    @classmethod
    async def get_consent(
        cls,
        db: AsyncSession,
        language: str,
        *,
        fallback: bool = False,
    ) -> PersonalDataConsent | None:
        lang = cls._normalize_language(language)
        consent = await get_personal_data_consent(db, lang)

        if consent or not fallback:
            return consent

        default_lang = cls._normalize_language(settings.DEFAULT_LANGUAGE)
        if lang != default_lang:
            return await get_personal_data_consent(db, default_lang)

        return consent

    @classmethod
    async def save_consent(
        cls,
        db: AsyncSession,
        language: str,
        content: str,
    ) -> PersonalDataConsent:
        lang = cls._normalize_language(language)
        consent = await upsert_personal_data_consent(db, lang, content, enable_if_new=True)
        logger.info('✅ Согласие на обработку ПД обновлено для языка', lang=lang)
        return consent

    @classmethod
    async def set_enabled(
        cls,
        db: AsyncSession,
        language: str,
        enabled: bool,
    ) -> PersonalDataConsent:
        lang = cls._normalize_language(language)
        return await set_personal_data_consent_enabled(db, lang, enabled)
