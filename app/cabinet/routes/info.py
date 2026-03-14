"""Info pages routes for cabinet - FAQ, rules, privacy policy, etc."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.rules import get_current_rules_content, get_rules_by_language
from app.database.models import User
from app.services.faq_service import FaqService
from app.services.privacy_policy_service import PrivacyPolicyService
from app.services.public_offer_service import PublicOfferService

from ..dependencies import get_cabinet_db, get_current_cabinet_user


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/info', tags=['Cabinet Info'])

_LANGUAGE_META: dict[str, tuple[str, str]] = {
    'ru': ('Русский', '🇷🇺'),
    'en': ('English', '🇬🇧'),
    'ua': ('Українська', '🇺🇦'),
    'zh': ('中文', '🇨🇳'),
    'fa': ('فارسی', '🇮🇷'),
}


def _normalize_language_code(value: str | None) -> str:
    return (value or '').strip().lower().split('-', 1)[0]


def _get_available_language_codes() -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for code in settings.get_available_languages():
        normalized = _normalize_language_code(code)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        codes.append(normalized)
    return codes


# ============ Schemas ============


class FaqPageResponse(BaseModel):
    """FAQ page."""

    id: int
    title: str
    content: str
    order: int


class RulesResponse(BaseModel):
    """Service rules."""

    content: str
    updated_at: str | None = None


class PrivacyPolicyResponse(BaseModel):
    """Privacy policy."""

    content: str
    updated_at: str | None = None


class PublicOfferResponse(BaseModel):
    """Public offer."""

    content: str
    updated_at: str | None = None


class ServiceInfoResponse(BaseModel):
    """General service info."""

    name: str
    description: str | None = None
    support_email: str | None = None
    support_telegram: str | None = None
    website: str | None = None


class SupportConfigResponse(BaseModel):
    """Support/tickets configuration for miniapp."""

    tickets_enabled: bool
    support_type: str  # "tickets", "profile", "url"
    support_url: str | None = None
    support_username: str | None = None


# ============ Routes ============


@router.get('/faq', response_model=list[FaqPageResponse])
async def get_faq_pages(
    language: str = Query('ru', min_length=2, max_length=10),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get list of FAQ pages."""
    requested_lang = FaqService.normalize_language(language)
    pages = await FaqService.get_pages(
        db,
        requested_lang,
        include_inactive=False,  # Only active pages for cabinet
        fallback=True,
    )

    return [
        FaqPageResponse(
            id=page.id,
            title=page.title,
            content=page.content or '',
            order=page.display_order or 0,
        )
        for page in pages
    ]


@router.get('/faq/{page_id}', response_model=FaqPageResponse)
async def get_faq_page(
    page_id: int,
    language: str = Query('ru', min_length=2, max_length=10),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get a specific FAQ page by ID."""
    requested_lang = FaqService.normalize_language(language)
    page = await FaqService.get_page(
        db,
        page_id,
        requested_lang,
        include_inactive=False,
        fallback=True,
    )

    if not page:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='FAQ page not found',
        )

    return FaqPageResponse(
        id=page.id,
        title=page.title,
        content=page.content or '',
        order=page.display_order or 0,
    )


@router.get('/rules', response_model=RulesResponse)
async def get_rules(
    language: str = Query('ru', min_length=2, max_length=10),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get service rules - uses same function as bot."""
    requested_lang = language.split('-', maxsplit=1)[0].lower()

    # Use the same function as bot to ensure consistent content
    content = await get_current_rules_content(db, requested_lang)

    # Try to get updated_at from DB record
    rules = await get_rules_by_language(db, requested_lang)
    updated_at = None
    if rules and rules.updated_at:
        updated_at = rules.updated_at.isoformat()

    return RulesResponse(content=content, updated_at=updated_at)


@router.get('/privacy-policy', response_model=PrivacyPolicyResponse)
async def get_privacy_policy(
    language: str = Query('ru', min_length=2, max_length=10),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get privacy policy."""
    requested_lang = PrivacyPolicyService.normalize_language(language)
    policy = await PrivacyPolicyService.get_policy(db, requested_lang, fallback=True)

    if policy and policy.content:
        updated_at = policy.updated_at.isoformat() if policy.updated_at else None
        return PrivacyPolicyResponse(content=policy.content, updated_at=updated_at)

    # Return default policy if none found
    return PrivacyPolicyResponse(
        content="""# Политика конфиденциальности

Мы уважаем вашу конфиденциальность и защищаем ваши персональные данные.
""",
        updated_at=None,
    )


@router.get('/public-offer', response_model=PublicOfferResponse)
async def get_public_offer(
    language: str = Query('ru', min_length=2, max_length=10),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get public offer."""
    requested_lang = PublicOfferService.normalize_language(language)
    offer = await PublicOfferService.get_offer(db, requested_lang, fallback=True)

    if offer and offer.content:
        updated_at = offer.updated_at.isoformat() if offer.updated_at else None
        return PublicOfferResponse(content=offer.content, updated_at=updated_at)

    # Return default offer if none found
    return PublicOfferResponse(
        content="""# Публичная оферта

Условия использования сервиса.
""",
        updated_at=None,
    )


@router.get('/service', response_model=ServiceInfoResponse)
async def get_service_info():
    """Get general service information."""
    return ServiceInfoResponse(
        name=getattr(settings, 'SERVICE_NAME', None) or getattr(settings, 'BOT_NAME', 'VPN Service'),
        description=getattr(settings, 'SERVICE_DESCRIPTION', None),
        support_email=getattr(settings, 'SUPPORT_EMAIL', None),
        support_telegram=getattr(settings, 'SUPPORT_USERNAME', None) or getattr(settings, 'SUPPORT_TELEGRAM', None),
        website=getattr(settings, 'WEBSITE_URL', None),
    )


@router.get('/languages')
async def get_available_languages():
    """Get list of available languages."""
    codes = _get_available_language_codes()
    default_language = _normalize_language_code(getattr(settings, 'DEFAULT_LANGUAGE', 'ru') or 'ru')

    return {
        'languages': [
            {
                'code': code,
                'name': _LANGUAGE_META.get(code, (code.upper(), '🌐'))[0],
                'flag': _LANGUAGE_META.get(code, (code.upper(), '🌐'))[1],
            }
            for code in codes
        ],
        'default': default_language,
    }


@router.get('/user/language')
async def get_user_language(
    user: User = Depends(get_current_cabinet_user),
):
    """Get current user's language."""
    return {'language': user.language or 'ru'}


@router.patch('/user/language')
async def update_user_language(
    request: dict[str, str],
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update user's language preference."""
    requested_language = _normalize_language_code(request.get('language', 'ru'))
    available_languages = _get_available_language_codes()
    if requested_language not in available_languages:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Invalid language. Supported: {", ".join(available_languages)}',
        )

    user.language = requested_language
    await db.commit()
    await db.refresh(user)

    return {'language': user.language}


@router.get('/support-config', response_model=SupportConfigResponse)
async def get_support_config():
    """Get support/tickets configuration for cabinet."""
    # Use SUPPORT_SYSTEM_MODE setting (configurable from admin panel)
    support_mode = settings.get_support_system_mode()  # returns: tickets, contact, or both

    # Map support mode to support type for frontend
    # - "tickets" mode -> tickets only, no contact
    # - "contact" mode -> contact only (profile), no tickets
    # - "both" mode -> tickets enabled, contact available as fallback
    if support_mode == 'tickets':
        tickets_enabled = True
        support_type = 'tickets'
    elif support_mode == 'contact':
        tickets_enabled = False
        support_type = 'profile'
    else:  # both
        tickets_enabled = True
        support_type = 'tickets'

    return SupportConfigResponse(
        tickets_enabled=tickets_enabled,
        support_type=support_type,
        support_url=None,  # Cabinet doesn't use custom URLs
        support_username=settings.SUPPORT_USERNAME,  # Always return for fallback
    )
