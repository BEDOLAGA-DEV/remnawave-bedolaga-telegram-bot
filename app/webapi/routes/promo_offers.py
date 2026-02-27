from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.discount_offer import (
    count_discount_offers,
    get_offer_by_id,
    list_discount_offers,
    upsert_discount_offer,
)
from app.database.crud.promo_offer_log import list_promo_offer_logs
from app.database.crud.promo_offer_template import (
    get_promo_offer_template_by_id,
    list_promo_offer_templates,
    update_promo_offer_template,
)
from app.database.crud.user import get_user_by_telegram_id
from app.database.models import DiscountOffer, PromoOfferLog, PromoOfferTemplate, Subscription, User
from app.handlers.admin.messages import get_custom_users, get_target_users
from app.localization.texts import get_texts

from ..dependencies import get_db_session, require_api_token
from ..schemas.promo_offers import (
    PromoOfferBroadcastRequest,
    PromoOfferBroadcastResponse,
    PromoOfferCreateRequest,
    PromoOfferListResponse,
    PromoOfferLogListResponse,
    PromoOfferLogOfferInfo,
    PromoOfferLogResponse,
    PromoOfferResponse,
    PromoOfferSubscriptionInfo,
    PromoOfferTemplateListResponse,
    PromoOfferTemplateResponse,
    PromoOfferTemplateUpdateRequest,
    PromoOfferUserInfo,
)


router = APIRouter()



def _serialize_user(user: User | None) -> PromoOfferUserInfo | None:
    if not user:
        return None

    return PromoOfferUserInfo(
        id=user.id,
        telegram_id=user.telegram_id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        full_name=getattr(user, 'full_name', None),
    )


def _serialize_subscription(subscription: Subscription | None) -> PromoOfferSubscriptionInfo | None:
    if not subscription:
        return None

    return PromoOfferSubscriptionInfo(
        id=subscription.id,
        status=subscription.status,
        is_trial=subscription.is_trial,
        start_date=subscription.start_date,
        end_date=subscription.end_date,
        autopay_enabled=subscription.autopay_enabled,
    )


def _serialize_offer(offer: DiscountOffer) -> PromoOfferResponse:
    return PromoOfferResponse(
        id=offer.id,
        user_id=offer.user_id,
        subscription_id=offer.subscription_id,
        notification_type=offer.notification_type,
        discount_percent=offer.discount_percent,
        bonus_amount_kopeks=offer.bonus_amount_kopeks,
        expires_at=offer.expires_at,
        claimed_at=offer.claimed_at,
        is_active=offer.is_active,
        effect_type=offer.effect_type,
        extra_data=offer.extra_data or {},
        created_at=offer.created_at,
        updated_at=offer.updated_at,
        user=_serialize_user(getattr(offer, 'user', None)),
        subscription=_serialize_subscription(getattr(offer, 'subscription', None)),
    )


def _serialize_template(template: PromoOfferTemplate) -> PromoOfferTemplateResponse:
    return PromoOfferTemplateResponse(
        id=template.id,
        name=template.name,
        offer_type=template.offer_type,
        message_text=template.message_text,
        button_text=template.button_text,
        valid_hours=template.valid_hours,
        discount_percent=template.discount_percent,
        bonus_amount_kopeks=template.bonus_amount_kopeks,
        active_discount_hours=template.active_discount_hours,
        test_duration_hours=template.test_duration_hours,
        test_squad_uuids=[str(uuid) for uuid in (template.test_squad_uuids or [])],
        is_active=template.is_active,
        created_by=template.created_by,
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


def _build_log_response(entry: PromoOfferLog) -> PromoOfferLogResponse:
    user_info = _serialize_user(getattr(entry, 'user', None))

    offer = getattr(entry, 'offer', None)
    offer_info: PromoOfferLogOfferInfo | None = None
    if offer:
        offer_info = PromoOfferLogOfferInfo(
            id=offer.id,
            notification_type=offer.notification_type,
            discount_percent=offer.discount_percent,
            bonus_amount_kopeks=offer.bonus_amount_kopeks,
            effect_type=offer.effect_type,
            expires_at=offer.expires_at,
            claimed_at=offer.claimed_at,
            is_active=offer.is_active,
        )

    return PromoOfferLogResponse(
        id=entry.id,
        user_id=entry.user_id,
        offer_id=entry.offer_id,
        action=entry.action,
        source=entry.source,
        percent=entry.percent,
        effect_type=entry.effect_type,
        details=entry.details or {},
        created_at=entry.created_at,
        user=user_info,
        offer=offer_info,
    )


async def _resolve_target_users(db: AsyncSession, target: str) -> list[User]:
    normalized = target.strip().lower()
    if normalized.startswith('custom_'):
        criteria = normalized[len('custom_') :]
        return await get_custom_users(db, criteria)
    return await get_target_users(db, normalized)


@router.get('', response_model=PromoOfferListResponse)
async def list_promo_offers(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user_id: int | None = Query(None, ge=1),
    telegram_id: int | None = Query(None, ge=1),
    notification_type: str | None = Query(None, min_length=1),
    is_active: bool | None = Query(None),
) -> PromoOfferListResponse:
    texts = get_texts('ru')
    resolved_user_id = user_id
    if telegram_id is not None:
        user = await get_user_by_telegram_id(db, telegram_id)
        if not user:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail=texts.t('WEBAPI_PROMO_OFFERS_USER_NOT_FOUND', 'User not found'),
            )

        if resolved_user_id and resolved_user_id != user.id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=texts.t(
                    'WEBAPI_PROMO_OFFERS_TELEGRAM_ID_USER_ID_MISMATCH',
                    'telegram_id does not match the provided user_id',
                ),
            )

        resolved_user_id = user.id

    offers = await list_discount_offers(
        db,
        offset=offset,
        limit=limit,
        user_id=resolved_user_id,
        notification_type=notification_type,
        is_active=is_active,
    )
    total = await count_discount_offers(
        db,
        user_id=resolved_user_id,
        notification_type=notification_type,
        is_active=is_active,
    )

    return PromoOfferListResponse(
        items=[_serialize_offer(offer) for offer in offers],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post('', response_model=PromoOfferResponse, status_code=status.HTTP_201_CREATED)
async def create_promo_offer(
    payload: PromoOfferCreateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PromoOfferResponse:
    texts = get_texts('ru')
    if payload.discount_percent < 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t(
                'WEBAPI_PROMO_OFFERS_DISCOUNT_PERCENT_NON_NEGATIVE',
                'discount_percent must be non-negative',
            ),
        )
    if payload.bonus_amount_kopeks < 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t(
                'WEBAPI_PROMO_OFFERS_BONUS_AMOUNT_NON_NEGATIVE',
                'bonus_amount_kopeks must be non-negative',
            ),
        )
    if payload.valid_hours <= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t('WEBAPI_PROMO_OFFERS_VALID_HOURS_POSITIVE', 'valid_hours must be positive'),
        )
    if not payload.notification_type.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t(
                'WEBAPI_PROMO_OFFERS_NOTIFICATION_TYPE_REQUIRED',
                'notification_type must not be empty',
            ),
        )
    if not payload.effect_type.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t('WEBAPI_PROMO_OFFERS_EFFECT_TYPE_REQUIRED', 'effect_type must not be empty'),
        )

    target_user_id = payload.user_id
    user: User | None = None
    if payload.telegram_id is not None:
        user = await get_user_by_telegram_id(db, payload.telegram_id)
        if not user:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                texts.t('WEBAPI_PROMO_OFFERS_USER_NOT_FOUND', 'User not found'),
            )

        if target_user_id and target_user_id != user.id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                texts.t(
                    'WEBAPI_PROMO_OFFERS_USER_ID_TELEGRAM_ID_MISMATCH',
                    'Provided user_id does not match telegram_id',
                ),
            )

        target_user_id = user.id

    if target_user_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t(
                'WEBAPI_PROMO_OFFERS_USER_OR_TELEGRAM_REQUIRED',
                'user_id or telegram_id is required',
            ),
        )

    if user is None:
        user = await db.get(User, target_user_id)
    if not user:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_PROMO_OFFERS_USER_NOT_FOUND', 'User not found'),
        )

    if payload.subscription_id is not None:
        subscription = await db.get(Subscription, payload.subscription_id)
        if not subscription:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                texts.t('WEBAPI_PROMO_OFFERS_SUBSCRIPTION_NOT_FOUND', 'Subscription not found'),
            )
        if subscription.user_id != target_user_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                texts.t(
                    'WEBAPI_PROMO_OFFERS_SUBSCRIPTION_USER_MISMATCH',
                    'Subscription does not belong to the user',
                ),
            )

    offer = await upsert_discount_offer(
        db,
        user_id=target_user_id,
        subscription_id=payload.subscription_id,
        notification_type=payload.notification_type.strip(),
        discount_percent=payload.discount_percent,
        bonus_amount_kopeks=payload.bonus_amount_kopeks,
        valid_hours=payload.valid_hours,
        effect_type=payload.effect_type,
        extra_data=payload.extra_data,
    )

    await db.refresh(offer, attribute_names=['user', 'subscription'])

    return _serialize_offer(offer)


@router.post(
    '/broadcast',
    response_model=PromoOfferBroadcastResponse,
    status_code=status.HTTP_201_CREATED,
)
async def broadcast_promo_offers(
    payload: PromoOfferBroadcastRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PromoOfferBroadcastResponse:
    texts = get_texts('ru')
    if payload.discount_percent < 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t(
                'WEBAPI_PROMO_OFFERS_DISCOUNT_PERCENT_NON_NEGATIVE',
                'discount_percent must be non-negative',
            ),
        )
    if payload.bonus_amount_kopeks < 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t(
                'WEBAPI_PROMO_OFFERS_BONUS_AMOUNT_NON_NEGATIVE',
                'bonus_amount_kopeks must be non-negative',
            ),
        )
    if payload.valid_hours <= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t('WEBAPI_PROMO_OFFERS_VALID_HOURS_POSITIVE', 'valid_hours must be positive'),
        )
    if not payload.notification_type.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t(
                'WEBAPI_PROMO_OFFERS_NOTIFICATION_TYPE_REQUIRED',
                'notification_type must not be empty',
            ),
        )
    if not payload.effect_type.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t('WEBAPI_PROMO_OFFERS_EFFECT_TYPE_REQUIRED', 'effect_type must not be empty'),
        )

    recipients: dict[int, User] = {}

    target = payload.target
    if target:
        users = await _resolve_target_users(db, target)
        recipients.update({user.id: user for user in users if user and user.id})

    target_user_id = payload.user_id
    user: User | None = None
    if payload.telegram_id is not None:
        user = await get_user_by_telegram_id(db, payload.telegram_id)
        if not user:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                texts.t('WEBAPI_PROMO_OFFERS_USER_NOT_FOUND', 'User not found'),
            )

        if target_user_id and target_user_id != user.id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                texts.t(
                    'WEBAPI_PROMO_OFFERS_USER_ID_TELEGRAM_ID_MISMATCH',
                    'Provided user_id does not match telegram_id',
                ),
            )

        target_user_id = user.id

    if target_user_id is not None:
        if user is None:
            user = await db.get(User, target_user_id)
        if not user:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                texts.t('WEBAPI_PROMO_OFFERS_USER_NOT_FOUND', 'User not found'),
            )
        recipients[target_user_id] = user

    if not recipients:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t(
                'WEBAPI_PROMO_OFFERS_EMPTY_AUDIENCE',
                'Пустая аудитория: укажите target или конкретного пользователя',
            ),
        )

    if payload.subscription_id is not None:
        if len(recipients) > 1:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                texts.t(
                    'WEBAPI_PROMO_OFFERS_SUBSCRIPTION_ID_SINGLE_USER_ONLY',
                    'subscription_id можно использовать только при отправке одному пользователю',
                ),
            )
        sole_user = next(iter(recipients.values()))
        subscription = await db.get(Subscription, payload.subscription_id)
        if not subscription:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                texts.t('WEBAPI_PROMO_OFFERS_SUBSCRIPTION_NOT_FOUND', 'Subscription not found'),
            )
        if subscription.user_id != sole_user.id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                texts.t(
                    'WEBAPI_PROMO_OFFERS_SUBSCRIPTION_USER_MISMATCH',
                    'Subscription does not belong to the user',
                ),
            )

    created_offers = 0
    for user in recipients.values():
        offer = await upsert_discount_offer(
            db,
            user_id=user.id,
            subscription_id=payload.subscription_id,
            notification_type=payload.notification_type.strip(),
            discount_percent=payload.discount_percent,
            bonus_amount_kopeks=payload.bonus_amount_kopeks,
            valid_hours=payload.valid_hours,
            effect_type=payload.effect_type,
            extra_data=payload.extra_data,
        )
        if offer:
            created_offers += 1

    return PromoOfferBroadcastResponse(
        created_offers=created_offers,
        user_ids=list(recipients.keys()),
        target=payload.target,
    )


@router.get('/logs', response_model=PromoOfferLogListResponse)
async def get_promo_offer_logs(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user_id: int | None = Query(None, ge=1),
    offer_id: int | None = Query(None, ge=1),
    action: str | None = Query(None, min_length=1),
    source: str | None = Query(None, min_length=1),
) -> PromoOfferLogListResponse:
    logs, total = await list_promo_offer_logs(
        db,
        offset=offset,
        limit=limit,
        user_id=user_id,
        offer_id=offer_id,
        action=action,
        source=source,
    )

    return PromoOfferLogListResponse(
        items=[_build_log_response(entry) for entry in logs],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get('/templates', response_model=PromoOfferTemplateListResponse)
async def list_promo_offer_templates_endpoint(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PromoOfferTemplateListResponse:
    templates = await list_promo_offer_templates(db)
    return PromoOfferTemplateListResponse(items=[_serialize_template(template) for template in templates])


@router.get('/templates/{template_id}', response_model=PromoOfferTemplateResponse)
async def get_promo_offer_template_endpoint(
    template_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PromoOfferTemplateResponse:
    texts = get_texts('ru')
    template = await get_promo_offer_template_by_id(db, template_id)
    if not template:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t(
                'WEBAPI_PROMO_OFFERS_TEMPLATE_NOT_FOUND',
                'Promo offer template not found',
            ),
        )
    return _serialize_template(template)


@router.patch('/templates/{template_id}', response_model=PromoOfferTemplateResponse)
async def update_promo_offer_template_endpoint(
    template_id: int,
    payload: PromoOfferTemplateUpdateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PromoOfferTemplateResponse:
    texts = get_texts('ru')
    template = await get_promo_offer_template_by_id(db, template_id)
    if not template:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t(
                'WEBAPI_PROMO_OFFERS_TEMPLATE_NOT_FOUND',
                'Promo offer template not found',
            ),
        )

    if payload.valid_hours is not None and payload.valid_hours <= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t('WEBAPI_PROMO_OFFERS_VALID_HOURS_POSITIVE', 'valid_hours must be positive'),
        )
    if payload.active_discount_hours is not None and payload.active_discount_hours <= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t(
                'WEBAPI_PROMO_OFFERS_ACTIVE_DISCOUNT_HOURS_POSITIVE',
                'active_discount_hours must be positive',
            ),
        )
    if payload.test_duration_hours is not None and payload.test_duration_hours <= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t(
                'WEBAPI_PROMO_OFFERS_TEST_DURATION_HOURS_POSITIVE',
                'test_duration_hours must be positive',
            ),
        )
    if payload.discount_percent is not None and payload.discount_percent < 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t(
                'WEBAPI_PROMO_OFFERS_DISCOUNT_PERCENT_NON_NEGATIVE',
                'discount_percent must be non-negative',
            ),
        )
    if payload.bonus_amount_kopeks is not None and payload.bonus_amount_kopeks < 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t(
                'WEBAPI_PROMO_OFFERS_BONUS_AMOUNT_NON_NEGATIVE',
                'bonus_amount_kopeks must be non-negative',
            ),
        )

    if payload.test_squad_uuids is not None:
        normalized_squads = [str(uuid).strip() for uuid in payload.test_squad_uuids if str(uuid).strip()]
    else:
        normalized_squads = None

    updated_template = await update_promo_offer_template(
        db,
        template,
        name=payload.name,
        message_text=payload.message_text,
        button_text=payload.button_text,
        valid_hours=payload.valid_hours,
        discount_percent=payload.discount_percent,
        bonus_amount_kopeks=payload.bonus_amount_kopeks,
        active_discount_hours=payload.active_discount_hours,
        test_duration_hours=payload.test_duration_hours,
        test_squad_uuids=normalized_squads,
        is_active=payload.is_active,
    )

    return _serialize_template(updated_template)


@router.get('/{offer_id}', response_model=PromoOfferResponse)
async def get_promo_offer_endpoint(
    offer_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PromoOfferResponse:
    texts = get_texts('ru')
    offer = await get_offer_by_id(db, offer_id)
    if not offer:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_PROMO_OFFERS_OFFER_NOT_FOUND', 'Promo offer not found'),
        )

    return _serialize_offer(offer)
