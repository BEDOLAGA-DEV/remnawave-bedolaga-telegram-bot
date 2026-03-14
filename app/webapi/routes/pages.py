from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Query,
    Response,
    Security,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.faq import get_faq_page_by_id
from app.database.crud.rules import (
    clear_all_rules,
    create_or_update_rules,
    get_all_rules_versions,
    get_rules_by_language,
    restore_rules_version,
)
from app.database.models import ServiceRule
from app.services.faq_service import FaqService
from app.services.privacy_policy_service import PrivacyPolicyService
from app.services.public_offer_service import PublicOfferService

from ..dependencies import get_db_session, require_api_token
from ..schemas.pages import (
    FaqPageCreateRequest,
    FaqPageListResponse,
    FaqPageResponse,
    FaqPageUpdateRequest,
    FaqReorderRequest,
    FaqStatusResponse,
    FaqStatusUpdateRequest,
    RichTextPageResponse,
    RichTextPageUpdateRequest,
    ServiceRulesHistoryResponse,
    ServiceRulesResponse,
    ServiceRulesUpdateRequest,
)


router = APIRouter()


def _serialize_rich_page(
    *,
    requested_language: str,
    content: str,
    language: str,
    is_enabled: bool | None,
    created_at,
    updated_at,
    splitter,
) -> RichTextPageResponse:
    pages = splitter(content or '')
    return RichTextPageResponse(
        requested_language=requested_language,
        language=language,
        is_enabled=is_enabled,
        content=content or '',
        content_pages=pages,
        created_at=created_at,
        updated_at=updated_at,
    )


def _serialize_faq_page(page) -> FaqPageResponse:
    return FaqPageResponse(
        id=page.id,
        language=page.language,
        title=page.title,
        content=page.content,
        content_pages=FaqService.split_content_into_pages(page.content),
        display_order=page.display_order,
        is_active=page.is_active,
        created_at=page.created_at,
        updated_at=page.updated_at,
    )


def _serialize_rules(rule: ServiceRule) -> ServiceRulesResponse:
    return ServiceRulesResponse(
        id=rule.id,
        title=rule.title,
        content=rule.content,
        language=rule.language,
        is_active=rule.is_active,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


@router.get('/public-offer', response_model=RichTextPageResponse)
async def get_public_offer(
    _: object = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    language: str = Query('ru', min_length=2, max_length=10),
    fallback: bool = Query(True, description='Использовать запасной язык, если контента нет'),
    include_disabled: bool = Query(
        True,
        description='Возвращать контент даже если страница выключена',
    ),
) -> RichTextPageResponse:
    requested_lang = PublicOfferService.normalize_language(language)
    offer = await PublicOfferService.get_offer(db, requested_lang, fallback=fallback)

    if not offer:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Public offer not found')

    if not include_disabled and not offer.is_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Public offer disabled')

    return _serialize_rich_page(
        requested_language=requested_lang,
        language=offer.language,
        is_enabled=offer.is_enabled,
        content=offer.content or '',
        created_at=offer.created_at,
        updated_at=offer.updated_at,
        splitter=PublicOfferService.split_content_into_pages,
    )


@router.put('/public-offer', response_model=RichTextPageResponse)
async def update_public_offer(
    payload: RichTextPageUpdateRequest,
    _: object = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> RichTextPageResponse:
    lang = PublicOfferService.normalize_language(payload.language)
    offer = await PublicOfferService.save_offer(db, lang, payload.content)

    if payload.is_enabled is not None:
        offer = await PublicOfferService.set_enabled(db, lang, payload.is_enabled)

    refreshed = await PublicOfferService.get_offer(db, lang, fallback=False)
    offer = refreshed or offer

    return _serialize_rich_page(
        requested_language=lang,
        language=offer.language,
        is_enabled=offer.is_enabled,
        content=offer.content or '',
        created_at=offer.created_at,
        updated_at=offer.updated_at,
        splitter=PublicOfferService.split_content_into_pages,
    )


@router.get('/privacy-policy', response_model=RichTextPageResponse)
async def get_privacy_policy(
    _: object = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    language: str = Query('ru', min_length=2, max_length=10),
    fallback: bool = Query(True),
    include_disabled: bool = Query(True),
) -> RichTextPageResponse:
    requested_lang = PrivacyPolicyService.normalize_language(language)
    policy = await PrivacyPolicyService.get_policy(db, requested_lang, fallback=fallback)

    if not policy:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Privacy policy not found')

    if not include_disabled and not policy.is_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Privacy policy disabled')

    return _serialize_rich_page(
        requested_language=requested_lang,
        language=policy.language,
        is_enabled=policy.is_enabled,
        content=policy.content or '',
        created_at=policy.created_at,
        updated_at=policy.updated_at,
        splitter=PrivacyPolicyService.split_content_into_pages,
    )


@router.put('/privacy-policy', response_model=RichTextPageResponse)
async def update_privacy_policy(
    payload: RichTextPageUpdateRequest,
    _: object = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> RichTextPageResponse:
    lang = PrivacyPolicyService.normalize_language(payload.language)
    policy = await PrivacyPolicyService.save_policy(db, lang, payload.content)

    if payload.is_enabled is not None:
        policy = await PrivacyPolicyService.set_enabled(db, lang, payload.is_enabled)

    refreshed = await PrivacyPolicyService.get_policy(db, lang, fallback=False)
    policy = refreshed or policy

    return _serialize_rich_page(
        requested_language=lang,
        language=policy.language,
        is_enabled=policy.is_enabled,
        content=policy.content or '',
        created_at=policy.created_at,
        updated_at=policy.updated_at,
        splitter=PrivacyPolicyService.split_content_into_pages,
    )


@router.get('/faq', response_model=FaqPageListResponse)
async def list_faq_pages(
    _: object = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    language: str = Query('ru', min_length=2, max_length=10),
    include_inactive: bool = Query(True),
    fallback: bool = Query(True),
) -> FaqPageListResponse:
    requested_lang = FaqService.normalize_language(language)
    pages = await FaqService.get_pages(
        db,
        requested_lang,
        include_inactive=include_inactive,
        fallback=fallback,
    )

    resolved_language = requested_lang
    if pages:
        resolved_language = pages[0].language

    setting = await FaqService.get_setting(db, requested_lang, fallback=fallback)
    is_enabled = bool(setting.is_enabled) if setting else False
    if setting:
        resolved_language = setting.language

    serialized = [_serialize_faq_page(page) for page in pages]
    return FaqPageListResponse(
        requested_language=requested_lang,
        language=resolved_language,
        is_enabled=is_enabled,
        total=len(serialized),
        items=serialized,
    )


@router.get('/faq/status', response_model=FaqStatusResponse)
async def get_faq_status(
    _: object = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    language: str = Query('ru', min_length=2, max_length=10),
    fallback: bool = Query(True),
) -> FaqStatusResponse:
    requested_lang = FaqService.normalize_language(language)
    setting = await FaqService.get_setting(db, requested_lang, fallback=fallback)

    if not setting:
        return FaqStatusResponse(
            requested_language=requested_lang,
            language=requested_lang,
            is_enabled=False,
        )

    return FaqStatusResponse(
        requested_language=requested_lang,
        language=setting.language,
        is_enabled=bool(setting.is_enabled),
    )


@router.put('/faq/status', response_model=FaqStatusResponse)
async def update_faq_status(
    payload: FaqStatusUpdateRequest | None = Body(None),
    language: str = Query('ru', min_length=2, max_length=10),
    is_enabled: bool | None = Query(None),
    _: object = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> FaqStatusResponse:
    resolved_language = FaqService.normalize_language(payload.language if payload and payload.language else language)

    enabled_status = payload.is_enabled if payload else is_enabled
    if enabled_status is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Parameter 'is_enabled' is required")

    setting = await FaqService.set_enabled(db, resolved_language, enabled_status)

    return FaqStatusResponse(
        requested_language=resolved_language,
        language=setting.language,
        is_enabled=bool(setting.is_enabled),
    )


@router.post('/faq', response_model=FaqPageResponse, status_code=status.HTTP_201_CREATED)
async def create_faq_page(
    payload: FaqPageCreateRequest,
    _: object = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> FaqPageResponse:
    lang = FaqService.normalize_language(payload.language)
    is_active = True if payload.is_active is None else payload.is_active

    page = await FaqService.create_page(
        db,
        language=lang,
        title=payload.title,
        content=payload.content,
        display_order=payload.display_order,
        is_active=is_active,
    )

    return _serialize_faq_page(page)


@router.get('/faq/{page_id}', response_model=FaqPageResponse)
async def get_faq_page(
    page_id: int,
    _: object = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    language: str = Query('ru', min_length=2, max_length=10),
    include_inactive: bool = Query(True),
) -> FaqPageResponse:
    requested_lang = FaqService.normalize_language(language)
    page = await FaqService.get_page(
        db,
        page_id,
        requested_lang,
        include_inactive=include_inactive,
        fallback=True,
    )

    if not page:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'FAQ page not found')

    return _serialize_faq_page(page)


@router.put('/faq/{page_id}', response_model=FaqPageResponse)
async def update_faq_page(
    page_id: int,
    payload: FaqPageUpdateRequest,
    _: object = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> FaqPageResponse:
    page = await get_faq_page_by_id(db, page_id)

    if not page:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'FAQ page not found')

    updated = await FaqService.update_page(
        db,
        page,
        title=payload.title,
        content=payload.content,
        display_order=payload.display_order,
        is_active=payload.is_active,
    )

    return _serialize_faq_page(updated)


@router.delete('/faq/{page_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_faq_page(
    page_id: int,
    _: object = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    page = await get_faq_page_by_id(db, page_id)
    if not page:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'FAQ page not found')

    await FaqService.delete_page(db, page_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post('/faq/reorder', response_model=FaqPageListResponse)
async def reorder_faq_pages(
    payload: FaqReorderRequest,
    _: object = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> FaqPageListResponse:
    lang = FaqService.normalize_language(payload.language)

    ordered_payload = sorted(payload.items, key=lambda item: item.display_order)

    existing_pages = await FaqService.get_pages(
        db,
        lang,
        include_inactive=True,
        fallback=False,
    )
    pages_by_id = {page.id: page for page in existing_pages}

    pages: list[Any] = []
    for item in ordered_payload:
        page = pages_by_id.get(item.id)
        if not page:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f'FAQ page {item.id} not found for language {lang}',
            )
        pages.append(page)

    ordered_ids = {item.id for item in ordered_payload}
    remaining = [page for page in existing_pages if page.id not in ordered_ids]
    pages.extend(sorted(remaining, key=lambda page: (page.display_order, page.id)))

    await FaqService.reorder_pages(db, lang, pages)

    updated_pages = await FaqService.get_pages(
        db,
        lang,
        include_inactive=True,
        fallback=False,
    )
    setting = await FaqService.get_setting(db, lang, fallback=False)
    serialized = [_serialize_faq_page(page) for page in updated_pages]
    return FaqPageListResponse(
        requested_language=lang,
        language=lang,
        is_enabled=bool(setting.is_enabled) if setting else False,
        total=len(serialized),
        items=serialized,
    )


@router.get('/service-rules', response_model=ServiceRulesResponse)
async def get_service_rules(
    _: object = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    language: str = Query('ru', min_length=2, max_length=10),
    fallback: bool = Query(True),
) -> ServiceRulesResponse:
    requested_lang = language.split('-', maxsplit=1)[0].lower()
    rules = await get_rules_by_language(db, requested_lang)

    if not rules and fallback:
        default_lang = (settings.DEFAULT_LANGUAGE or 'ru').split('-')[0].lower()
        if default_lang != requested_lang:
            rules = await get_rules_by_language(db, default_lang)

    if not rules:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Service rules not found')

    return _serialize_rules(rules)


@router.put('/service-rules', response_model=ServiceRulesResponse)
async def update_service_rules(
    payload: ServiceRulesUpdateRequest,
    _: object = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ServiceRulesResponse:
    lang = payload.language.split('-')[0].lower()
    title = payload.title or 'Правила сервиса'
    rules = await create_or_update_rules(
        db,
        content=payload.content,
        language=lang,
        title=title,
    )

    return _serialize_rules(rules)


@router.delete('/service-rules', status_code=status.HTTP_204_NO_CONTENT)
async def clear_service_rules(
    _: object = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    language: str = Query('ru', min_length=2, max_length=10),
) -> Response:
    lang = language.split('-', maxsplit=1)[0].lower()
    await clear_all_rules(db, lang)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get('/service-rules/history', response_model=ServiceRulesHistoryResponse)
async def get_service_rules_history(
    _: object = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    language: str = Query('ru', min_length=2, max_length=10),
    limit: int = Query(10, ge=1, le=100),
) -> ServiceRulesHistoryResponse:
    lang = language.split('-', maxsplit=1)[0].lower()
    history = await get_all_rules_versions(db, lang, limit=limit)
    items = [_serialize_rules(item) for item in history]
    return ServiceRulesHistoryResponse(
        language=lang,
        total=len(items),
        items=items,
    )


@router.post(
    '/service-rules/history/{rule_id}/restore',
    response_model=ServiceRulesResponse,
    status_code=status.HTTP_201_CREATED,
)
async def restore_service_rules_version(
    rule_id: int,
    _: object = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    language: str = Query('ru', min_length=2, max_length=10),
) -> ServiceRulesResponse:
    lang = language.split('-', maxsplit=1)[0].lower()
    restored = await restore_rules_version(db, rule_id, language=lang)
    if not restored:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Rules version not found')
    return _serialize_rules(restored)
