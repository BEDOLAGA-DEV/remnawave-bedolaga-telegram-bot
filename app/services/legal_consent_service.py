"""Согласие с офертой и политикой при первой авторизации в кабинете.

В боте согласие с правилами спрашивается при регистрации, но живёт только в FSM и
никуда не сохраняется. В кабинете новый пользователь создавался вообще молча: зашёл
через Telegram — аккаунт готов, никаких документов ему не показывали.

Здесь один источник правды на весь кабинет: какие документы требуют галочки, нужна
ли она вообще и как записать факт согласия. Ключевое правило — требовать согласие
можно только с тем, что пользователь способен прочитать: если документ выключен или
скрыт из веба, галочки по нему нет. Иначе установка без заполненной оферты
заблокировала бы регистрацию вообще всем.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import LegalConsent, User
from app.services.privacy_policy_service import PrivacyPolicyService
from app.services.public_offer_service import PublicOfferService
from app.utils.display_mode import is_visible_in_web


logger = structlog.get_logger(__name__)

PUBLIC_OFFER = 'public_offer'
PRIVACY_POLICY = 'privacy_policy'

# Порядок важен: в таком виде чекбоксы показываются пользователю.
KNOWN_DOCUMENTS: tuple[str, ...] = (PUBLIC_OFFER, PRIVACY_POLICY)


@dataclass(slots=True)
class LegalConsentRequirement:
    """Что кабинет должен показать на экране первой авторизации."""

    required: bool
    prechecked: bool
    documents: list[str]


async def _document_is_available(db: AsyncSession, document: str, language: str) -> bool:
    """Есть ли документ, который пользователь реально сможет открыть."""
    if document == PUBLIC_OFFER:
        if not is_visible_in_web(settings.PUBLIC_OFFER_DISPLAY_MODE):
            return False
        offer = await PublicOfferService.get_offer(db, PublicOfferService.normalize_language(language), fallback=True)
        return bool(offer and (offer.content or '').strip())

    if document == PRIVACY_POLICY:
        if not is_visible_in_web(settings.PRIVACY_POLICY_DISPLAY_MODE):
            return False
        policy = await PrivacyPolicyService.get_policy(
            db, PrivacyPolicyService.normalize_language(language), fallback=True
        )
        return bool(policy and (policy.content or '').strip())

    return False


async def get_requirement(db: AsyncSession, language: str = 'ru') -> LegalConsentRequirement:
    """Требование согласия для НОВОГО пользователя кабинета."""
    if not settings.CABINET_REQUIRE_LEGAL_CONSENT:
        return LegalConsentRequirement(required=False, prechecked=False, documents=[])

    documents: list[str] = []
    for document in KNOWN_DOCUMENTS:
        try:
            if await _document_is_available(db, document, language):
                documents.append(document)
        except Exception as error:  # pragma: no cover - defensive
            # Сбой чтения документа не должен закрывать вход в кабинет: без документа
            # галочки по нему просто не будет.
            logger.warning('Не удалось проверить доступность документа', document=document, error=str(error))

    return LegalConsentRequirement(
        required=bool(documents),
        prechecked=bool(settings.CABINET_LEGAL_CONSENT_PRECHECKED),
        documents=documents,
    )


def missing_documents(required: list[str], accepted: list[str] | None) -> list[str]:
    """Какие из обязательных документов пользователь не отметил."""
    accepted_set = {item.strip() for item in (accepted or []) if isinstance(item, str)}
    return [document for document in required if document not in accepted_set]


async def record_consent(
    db: AsyncSession,
    user: User,
    documents: list[str],
    *,
    source: str,
    ip_address: str | None = None,
    commit: bool = True,
) -> None:
    """Записать факт согласия. Сбой записи не должен ронять регистрацию."""
    if not documents:
        return

    try:
        for document in documents:
            db.add(LegalConsent(user_id=user.id, document=document, source=source, ip_address=ip_address))
        if commit:
            await db.commit()
    except Exception as error:  # pragma: no cover - defensive
        logger.error('Не удалось записать согласие с документами', user_id=user.id, error=str(error))
        if commit:
            await db.rollback()
