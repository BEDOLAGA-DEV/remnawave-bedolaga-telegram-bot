import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.faq import (
    create_faq_page,
    delete_faq_page,
    get_faq_page_by_id,
    get_faq_pages,
    get_faq_setting,
    set_faq_enabled,
    update_faq_page,
)
from app.database.crud.privacy_policy import (
    get_privacy_policy,
    upsert_privacy_policy,
)
from app.database.crud.public_offer import (
    get_public_offer,
    upsert_public_offer,
)
from app.database.crud.recurrent_payments import (
    get_recurrent_payments,
    upsert_recurrent_payments,
)
from app.database.crud.rules import clear_all_rules, create_or_update_rules, get_rules_by_language
from app.database.models import FaqPage, User
from app.services.system_settings_service import bot_configuration_service
from app.utils.display_mode import normalize_display_mode

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/legal-pages', tags=['Cabinet Admin Legal Pages'])

_DISPLAY_MODE_KEYS = {
    'privacy-policy': 'PRIVACY_POLICY_DISPLAY_MODE',
    'public-offer': 'PUBLIC_OFFER_DISPLAY_MODE',
    'recurrent-payments': 'RECURRENT_PAYMENTS_DISPLAY_MODE',
    'rules': 'SERVICE_RULES_DISPLAY_MODE',
    'faq': 'FAQ_DISPLAY_MODE',
}


def _normalize_lang(language: str) -> str:
    return (language or '').strip().lower().split('-', 1)[0]


def _available_languages() -> list[str]:
    codes: list[str] = []
    for code in settings.get_available_languages():
        normalized = _normalize_lang(code)
        if normalized and normalized not in codes:
            codes.append(normalized)
    return codes


def _require_language(language: str) -> str:
    lang = _normalize_lang(language)
    if lang not in _available_languages():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f'Unsupported language: {lang}',
        )
    return lang


def _display_mode_env_locked(page_key: str) -> bool:
    return bot_configuration_service.is_env_overridden(_DISPLAY_MODE_KEYS[page_key])


def _check_display_mode_writable(page_key: str, value: str | None) -> None:
    if not value:
        return
    key = _DISPLAY_MODE_KEYS[page_key]
    normalized = normalize_display_mode(value)
    current = normalize_display_mode(getattr(settings, key, None))
    if normalized != current and _display_mode_env_locked(page_key):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='display_mode is locked by environment variable',
        )


async def _set_display_mode(db: AsyncSession, page_key: str, value: str) -> None:
    key = _DISPLAY_MODE_KEYS[page_key]
    normalized = normalize_display_mode(value)
    _check_display_mode_writable(page_key, value)
    await bot_configuration_service.set_value(db, key, normalized)
    await db.commit()


class LegalDocumentItem(BaseModel):
    language: str
    content: str
    is_enabled: bool
    updated_at: str | None = None


class LegalDocumentResponse(BaseModel):
    display_mode: str
    display_mode_env_locked: bool
    items: list[LegalDocumentItem]


class LegalDocumentItemUpdate(BaseModel):
    language: str = Field(min_length=2, max_length=10)
    content: str = ''
    is_enabled: bool = True


class LegalDocumentUpdateRequest(BaseModel):
    display_mode: str | None = Field(None, pattern=r'^(bot|web|both)$')
    items: list[LegalDocumentItemUpdate] | None = None


class RulesItem(BaseModel):
    language: str
    content: str
    updated_at: str | None = None


class RulesResponse(BaseModel):
    display_mode: str
    display_mode_env_locked: bool
    items: list[RulesItem]


class RulesItemUpdate(BaseModel):
    language: str = Field(min_length=2, max_length=10)
    content: str = ''


class RulesUpdateRequest(BaseModel):
    display_mode: str | None = Field(None, pattern=r'^(bot|web|both)$')
    items: list[RulesItemUpdate] | None = None


class FaqSettingItem(BaseModel):
    language: str
    is_enabled: bool


class FaqPageItem(BaseModel):
    id: int
    language: str
    title: str
    content: str
    display_order: int
    is_active: bool
    updated_at: str | None = None


class FaqResponse(BaseModel):
    display_mode: str
    display_mode_env_locked: bool
    settings: list[FaqSettingItem]
    pages: list[FaqPageItem]


class FaqUpdateRequest(BaseModel):
    display_mode: str | None = Field(None, pattern=r'^(bot|web|both)$')
    settings: list[FaqSettingItem] | None = None


class FaqPageCreateRequest(BaseModel):
    language: str = Field(min_length=2, max_length=10)
    title: str = Field(min_length=1, max_length=255)
    content: str = ''
    display_order: int | None = Field(None, ge=0)
    is_active: bool = True


class FaqPageUpdateRequest(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=255)
    content: str | None = None
    display_order: int | None = Field(None, ge=0)
    is_active: bool | None = None


def _faq_page_item(page: FaqPage) -> FaqPageItem:
    return FaqPageItem(
        id=page.id,
        language=page.language,
        title=page.title,
        content=page.content or '',
        display_order=page.display_order or 0,
        is_active=page.is_active,
        updated_at=page.updated_at.isoformat() if page.updated_at else None,
    )


async def _build_privacy_response(db: AsyncSession) -> LegalDocumentResponse:
    items: list[LegalDocumentItem] = []
    for lang in _available_languages():
        policy = await get_privacy_policy(db, lang)
        items.append(
            LegalDocumentItem(
                language=lang,
                content=policy.content if policy else '',
                is_enabled=bool(policy.is_enabled) if policy else False,
                updated_at=policy.updated_at.isoformat() if policy and policy.updated_at else None,
            )
        )
    return LegalDocumentResponse(
        display_mode=normalize_display_mode(settings.PRIVACY_POLICY_DISPLAY_MODE),
        display_mode_env_locked=_display_mode_env_locked('privacy-policy'),
        items=items,
    )


async def _build_offer_response(db: AsyncSession) -> LegalDocumentResponse:
    items: list[LegalDocumentItem] = []
    for lang in _available_languages():
        offer = await get_public_offer(db, lang)
        items.append(
            LegalDocumentItem(
                language=lang,
                content=offer.content if offer else '',
                is_enabled=bool(offer.is_enabled) if offer else False,
                updated_at=offer.updated_at.isoformat() if offer and offer.updated_at else None,
            )
        )
    return LegalDocumentResponse(
        display_mode=normalize_display_mode(settings.PUBLIC_OFFER_DISPLAY_MODE),
        display_mode_env_locked=_display_mode_env_locked('public-offer'),
        items=items,
    )


async def _build_recurrent_payments_response(db: AsyncSession) -> LegalDocumentResponse:
    items: list[LegalDocumentItem] = []
    for lang in _available_languages():
        document = await get_recurrent_payments(db, lang)
        items.append(
            LegalDocumentItem(
                language=lang,
                content=document.content if document else '',
                is_enabled=bool(document.is_enabled) if document else False,
                updated_at=document.updated_at.isoformat() if document and document.updated_at else None,
            )
        )
    return LegalDocumentResponse(
        display_mode=normalize_display_mode(settings.RECURRENT_PAYMENTS_DISPLAY_MODE),
        display_mode_env_locked=_display_mode_env_locked('recurrent-payments'),
        items=items,
    )


async def _build_rules_response(db: AsyncSession) -> RulesResponse:
    items: list[RulesItem] = []
    for lang in _available_languages():
        rules = await get_rules_by_language(db, lang)
        items.append(
            RulesItem(
                language=lang,
                content=rules.content if rules else '',
                updated_at=rules.updated_at.isoformat() if rules and rules.updated_at else None,
            )
        )
    return RulesResponse(
        display_mode=normalize_display_mode(settings.SERVICE_RULES_DISPLAY_MODE),
        display_mode_env_locked=_display_mode_env_locked('rules'),
        items=items,
    )


async def _build_faq_response(db: AsyncSession) -> FaqResponse:
    settings_items: list[FaqSettingItem] = []
    pages: list[FaqPageItem] = []
    for lang in _available_languages():
        setting = await get_faq_setting(db, lang)
        settings_items.append(
            FaqSettingItem(
                language=lang,
                is_enabled=bool(setting.is_enabled) if setting else False,
            )
        )
        for page in await get_faq_pages(db, lang, include_inactive=True):
            pages.append(_faq_page_item(page))
    return FaqResponse(
        display_mode=normalize_display_mode(settings.FAQ_DISPLAY_MODE),
        display_mode_env_locked=_display_mode_env_locked('faq'),
        settings=settings_items,
        pages=pages,
    )


@router.get('/privacy-policy', response_model=LegalDocumentResponse)
async def get_privacy_policy_admin(
    admin: User = Depends(require_permission('info_pages:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> LegalDocumentResponse:
    return await _build_privacy_response(db)


@router.put('/privacy-policy', response_model=LegalDocumentResponse)
async def update_privacy_policy_admin(
    request: LegalDocumentUpdateRequest,
    admin: User = Depends(require_permission('info_pages:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> LegalDocumentResponse:
    items = request.items or []
    languages = [_require_language(item.language) for item in items]
    _check_display_mode_writable('privacy-policy', request.display_mode)
    for lang, item in zip(languages, items, strict=True):
        try:
            await upsert_privacy_policy(db, lang, item.content, is_enabled=item.is_enabled)
        except IntegrityError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f'Privacy policy for language {lang} already exists',
            )
    if request.display_mode:
        await _set_display_mode(db, 'privacy-policy', request.display_mode)
    logger.info('Admin updated privacy policy via cabinet', admin_id=admin.id)
    return await _build_privacy_response(db)


@router.get('/public-offer', response_model=LegalDocumentResponse)
async def get_public_offer_admin(
    admin: User = Depends(require_permission('info_pages:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> LegalDocumentResponse:
    return await _build_offer_response(db)


@router.put('/public-offer', response_model=LegalDocumentResponse)
async def update_public_offer_admin(
    request: LegalDocumentUpdateRequest,
    admin: User = Depends(require_permission('info_pages:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> LegalDocumentResponse:
    items = request.items or []
    languages = [_require_language(item.language) for item in items]
    _check_display_mode_writable('public-offer', request.display_mode)
    for lang, item in zip(languages, items, strict=True):
        try:
            await upsert_public_offer(db, lang, item.content, is_enabled=item.is_enabled)
        except IntegrityError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f'Public offer for language {lang} already exists',
            )
    if request.display_mode:
        await _set_display_mode(db, 'public-offer', request.display_mode)
    logger.info('Admin updated public offer via cabinet', admin_id=admin.id)
    return await _build_offer_response(db)


@router.get('/recurrent-payments', response_model=LegalDocumentResponse)
async def get_recurrent_payments_admin(
    admin: User = Depends(require_permission('info_pages:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> LegalDocumentResponse:
    return await _build_recurrent_payments_response(db)


@router.put('/recurrent-payments', response_model=LegalDocumentResponse)
async def update_recurrent_payments_admin(
    request: LegalDocumentUpdateRequest,
    admin: User = Depends(require_permission('info_pages:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> LegalDocumentResponse:
    items = request.items or []
    languages = [_require_language(item.language) for item in items]
    _check_display_mode_writable('recurrent-payments', request.display_mode)
    for lang, item in zip(languages, items, strict=True):
        try:
            await upsert_recurrent_payments(db, lang, item.content, is_enabled=item.is_enabled)
        except IntegrityError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f'Recurring-payments document for language {lang} already exists',
            )
    if request.display_mode:
        await _set_display_mode(db, 'recurrent-payments', request.display_mode)
    logger.info('Admin updated recurring-payments document via cabinet', admin_id=admin.id)
    return await _build_recurrent_payments_response(db)


@router.get('/rules', response_model=RulesResponse)
async def get_rules_admin(
    admin: User = Depends(require_permission('info_pages:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> RulesResponse:
    return await _build_rules_response(db)


@router.put('/rules', response_model=RulesResponse)
async def update_rules_admin(
    request: RulesUpdateRequest,
    admin: User = Depends(require_permission('info_pages:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> RulesResponse:
    items = request.items or []
    languages = [_require_language(item.language) for item in items]
    _check_display_mode_writable('rules', request.display_mode)
    for lang, item in zip(languages, items, strict=True):
        if item.content.strip():
            await create_or_update_rules(db, item.content, lang)
        else:
            await clear_all_rules(db, lang)
    if request.display_mode:
        await _set_display_mode(db, 'rules', request.display_mode)
    logger.info('Admin updated service rules via cabinet', admin_id=admin.id)
    return await _build_rules_response(db)


@router.get('/faq', response_model=FaqResponse)
async def get_faq_admin(
    admin: User = Depends(require_permission('info_pages:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> FaqResponse:
    return await _build_faq_response(db)


@router.put('/faq', response_model=FaqResponse)
async def update_faq_admin(
    request: FaqUpdateRequest,
    admin: User = Depends(require_permission('info_pages:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> FaqResponse:
    items = request.settings or []
    languages = [_require_language(item.language) for item in items]
    _check_display_mode_writable('faq', request.display_mode)
    for lang, item in zip(languages, items, strict=True):
        try:
            await set_faq_enabled(db, lang, item.is_enabled)
        except IntegrityError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f'FAQ setting for language {lang} already exists',
            )
    if request.display_mode:
        await _set_display_mode(db, 'faq', request.display_mode)
    logger.info('Admin updated FAQ settings via cabinet', admin_id=admin.id)
    return await _build_faq_response(db)


@router.post('/faq/pages', response_model=FaqPageItem, status_code=status.HTTP_201_CREATED)
async def create_faq_page_admin(
    request: FaqPageCreateRequest,
    admin: User = Depends(require_permission('info_pages:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> FaqPageItem:
    page = await create_faq_page(
        db,
        language=_require_language(request.language),
        title=request.title,
        content=request.content,
        display_order=request.display_order,
        is_active=request.is_active,
    )
    logger.info('Admin created FAQ page via cabinet', admin_id=admin.id, page_id=page.id)
    return _faq_page_item(page)


@router.put('/faq/pages/{page_id}', response_model=FaqPageItem)
async def update_faq_page_admin(
    page_id: int,
    request: FaqPageUpdateRequest,
    admin: User = Depends(require_permission('info_pages:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> FaqPageItem:
    page = await get_faq_page_by_id(db, page_id)
    if not page:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='FAQ page not found',
        )
    updated = await update_faq_page(
        db,
        page,
        title=request.title,
        content=request.content,
        display_order=request.display_order,
        is_active=request.is_active,
    )
    logger.info('Admin updated FAQ page via cabinet', admin_id=admin.id, page_id=page_id)
    return _faq_page_item(updated)


@router.delete('/faq/pages/{page_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_faq_page_admin(
    page_id: int,
    admin: User = Depends(require_permission('info_pages:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> None:
    page = await get_faq_page_by_id(db, page_id)
    if not page:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='FAQ page not found',
        )
    await delete_faq_page(db, page_id)
    logger.info('Admin deleted FAQ page via cabinet', admin_id=admin.id, page_id=page_id)
