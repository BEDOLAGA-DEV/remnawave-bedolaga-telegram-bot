from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.localization.texts import get_texts
from app.database.crud.promocode import (
    create_promocode,
    delete_promocode,
    get_promocode_by_code,
    get_promocode_by_id,
    get_promocode_statistics,
    get_promocodes_count,
    get_promocodes_list,
    update_promocode,
)
from app.database.models import PromoCode, PromoCodeType, PromoCodeUse

from ..dependencies import get_db_session, require_api_token
from ..schemas.promocodes import (
    PromoCodeCreateRequest,
    PromoCodeDetailResponse,
    PromoCodeListResponse,
    PromoCodeRecentUse,
    PromoCodeResponse,
    PromoCodeUpdateRequest,
)


router = APIRouter()


def _normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None

    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC)

    if value.tzinfo is not None:
        return value

    return value


def _serialize_promocode(promocode: PromoCode) -> PromoCodeResponse:
    promo_type = PromoCodeType(promocode.type)
    return PromoCodeResponse(
        id=promocode.id,
        code=promocode.code,
        type=promo_type,
        balance_bonus_kopeks=promocode.balance_bonus_kopeks,
        balance_bonus_rubles=round(promocode.balance_bonus_kopeks / 100, 2),
        subscription_days=promocode.subscription_days,
        max_uses=promocode.max_uses,
        current_uses=promocode.current_uses,
        uses_left=promocode.uses_left,
        is_active=promocode.is_active,
        is_valid=promocode.is_valid,
        valid_from=promocode.valid_from,
        valid_until=promocode.valid_until,
        created_by=promocode.created_by,
        created_at=promocode.created_at,
        updated_at=promocode.updated_at,
    )


def _serialize_recent_use(use: PromoCodeUse) -> PromoCodeRecentUse:
    return PromoCodeRecentUse(
        id=use.id,
        user_id=use.user_id,
        user_username=getattr(use, 'user_username', None),
        user_full_name=getattr(use, 'user_full_name', None),
        user_telegram_id=getattr(use, 'user_telegram_id', None),
        used_at=use.used_at,
    )


def _validate_create_payload(payload: PromoCodeCreateRequest) -> None:
    code = payload.code.strip()
    if not code:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=get_texts('ru').t('WEBAPI_PROMOCODES_CODE_MUST_NOT_BE_EMPTY', 'Code must not be empty'),
        )

    normalized_valid_from = _normalize_datetime(payload.valid_from)
    normalized_valid_until = _normalize_datetime(payload.valid_until)

    if payload.type == PromoCodeType.BALANCE and payload.balance_bonus_kopeks <= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=get_texts('ru').t(
                'WEBAPI_PROMOCODES_BALANCE_BONUS_MUST_BE_POSITIVE_FOR_BALANCE_PROMO_CODES',
                'Balance bonus must be positive for balance promo codes',
            ),
        )

    if (
        payload.type in {PromoCodeType.SUBSCRIPTION_DAYS, PromoCodeType.TRIAL_SUBSCRIPTION}
        and payload.subscription_days <= 0
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=get_texts('ru').t(
                'WEBAPI_PROMOCODES_SUBSCRIPTION_DAYS_MUST_BE_POSITIVE_FOR_THIS_PROMO_CODE_TYPE',
                'Subscription days must be positive for this promo code type',
            ),
        )

    if normalized_valid_from and normalized_valid_until and normalized_valid_from > normalized_valid_until:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=get_texts('ru').t(
                'WEBAPI_PROMOCODES_VALID_FROM_CANNOT_BE_GREATER_THAN_VALID_UNTIL',
                'valid_from cannot be greater than valid_until',
            ),
        )


def _validate_update_payload(payload: PromoCodeUpdateRequest, promocode: PromoCode) -> None:
    if payload.code is not None and not payload.code.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=get_texts('ru').t('WEBAPI_PROMOCODES_CODE_MUST_NOT_BE_EMPTY', 'Code must not be empty'),
        )

    if payload.type is not None:
        new_type = payload.type
    else:
        new_type = PromoCodeType(promocode.type)

    balance_bonus = (
        payload.balance_bonus_kopeks if payload.balance_bonus_kopeks is not None else promocode.balance_bonus_kopeks
    )
    subscription_days = (
        payload.subscription_days if payload.subscription_days is not None else promocode.subscription_days
    )

    if new_type == PromoCodeType.BALANCE and balance_bonus <= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=get_texts('ru').t(
                'WEBAPI_PROMOCODES_BALANCE_BONUS_MUST_BE_POSITIVE_FOR_BALANCE_PROMO_CODES',
                'Balance bonus must be positive for balance promo codes',
            ),
        )

    if new_type in {PromoCodeType.SUBSCRIPTION_DAYS, PromoCodeType.TRIAL_SUBSCRIPTION} and subscription_days <= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=get_texts('ru').t(
                'WEBAPI_PROMOCODES_SUBSCRIPTION_DAYS_MUST_BE_POSITIVE_FOR_THIS_PROMO_CODE_TYPE',
                'Subscription days must be positive for this promo code type',
            ),
        )

    valid_from = _normalize_datetime(payload.valid_from) if payload.valid_from is not None else promocode.valid_from
    valid_until = _normalize_datetime(payload.valid_until) if payload.valid_until is not None else promocode.valid_until

    if valid_from and valid_until and valid_from > valid_until:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=get_texts('ru').t(
                'WEBAPI_PROMOCODES_VALID_FROM_CANNOT_BE_GREATER_THAN_VALID_UNTIL',
                'valid_from cannot be greater than valid_until',
            ),
        )

    if payload.max_uses is not None and payload.max_uses != 0 and payload.max_uses < promocode.current_uses:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=get_texts('ru').t(
                'WEBAPI_PROMOCODES_MAX_USES_CANNOT_BE_LESS_THAN_CURRENT_USES',
                'max_uses cannot be less than current uses',
            ),
        )


@router.get('', response_model=PromoCodeListResponse)
async def list_promocodes(
    _: Any = Depends(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    is_active: bool | None = Query(default=None),
) -> PromoCodeListResponse:
    total = await get_promocodes_count(db, is_active=is_active) or 0
    promocodes = await get_promocodes_list(db, offset=offset, limit=limit, is_active=is_active)

    return PromoCodeListResponse(
        items=[_serialize_promocode(promocode) for promocode in promocodes],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get('/{promocode_id}', response_model=PromoCodeDetailResponse)
async def get_promocode(
    promocode_id: int,
    _: Any = Depends(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PromoCodeDetailResponse:
    promocode = await get_promocode_by_id(db, promocode_id)
    if not promocode:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_PROMOCODES_NOT_FOUND', 'Promo code not found'),
        )

    stats = await get_promocode_statistics(db, promocode_id)
    base = _serialize_promocode(promocode)
    recent_uses = [_serialize_recent_use(use) for use in stats.get('recent_uses', [])]

    return PromoCodeDetailResponse(
        **base.dict(),
        total_uses=stats.get('total_uses', 0),
        today_uses=stats.get('today_uses', 0),
        recent_uses=recent_uses,
    )


@router.post('', response_model=PromoCodeResponse, status_code=status.HTTP_201_CREATED)
async def create_promocode_endpoint(
    payload: PromoCodeCreateRequest,
    _: Any = Depends(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PromoCodeResponse:
    _validate_create_payload(payload)

    normalized_code = payload.code.strip().upper()
    normalized_valid_from = _normalize_datetime(payload.valid_from)
    normalized_valid_until = _normalize_datetime(payload.valid_until)

    existing = await get_promocode_by_code(db, normalized_code)
    if existing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=get_texts('ru').t(
                'WEBAPI_PROMOCODES_CODE_ALREADY_EXISTS',
                'Promo code with this code already exists',
            ),
        )

    creator_id = payload.created_by if payload.created_by is not None and payload.created_by > 0 else None

    # 0 means unlimited — convert to large number for is_valid check (current_uses < max_uses)
    effective_max_uses = 999999 if payload.max_uses == 0 else payload.max_uses

    promocode = await create_promocode(
        db,
        code=normalized_code,
        type=payload.type,
        balance_bonus_kopeks=payload.balance_bonus_kopeks,
        subscription_days=payload.subscription_days,
        max_uses=effective_max_uses,
        valid_until=normalized_valid_until,
        created_by=creator_id,
    )

    update_fields = {}
    if normalized_valid_from is not None:
        update_fields['valid_from'] = normalized_valid_from
    if payload.is_active is not None and payload.is_active != promocode.is_active:
        update_fields['is_active'] = payload.is_active
    if normalized_valid_until is not None:
        update_fields['valid_until'] = normalized_valid_until

    if update_fields:
        promocode = await update_promocode(db, promocode, **update_fields)

    return _serialize_promocode(promocode)


@router.patch('/{promocode_id}', response_model=PromoCodeResponse)
async def update_promocode_endpoint(
    promocode_id: int,
    payload: PromoCodeUpdateRequest,
    _: Any = Depends(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PromoCodeResponse:
    promocode = await get_promocode_by_id(db, promocode_id)
    if not promocode:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_PROMOCODES_NOT_FOUND', 'Promo code not found'),
        )

    _validate_update_payload(payload, promocode)

    updates: dict[str, Any] = {}

    if payload.code is not None:
        normalized_code = payload.code.strip().upper()
        if normalized_code != promocode.code:
            existing = await get_promocode_by_code(db, normalized_code)
            if existing and existing.id != promocode_id:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail=get_texts('ru').t(
                        'WEBAPI_PROMOCODES_CODE_ALREADY_EXISTS',
                        'Promo code with this code already exists',
                    ),
                )
        updates['code'] = normalized_code

    if payload.type is not None:
        updates['type'] = payload.type.value

    if payload.balance_bonus_kopeks is not None:
        updates['balance_bonus_kopeks'] = payload.balance_bonus_kopeks

    if payload.subscription_days is not None:
        updates['subscription_days'] = payload.subscription_days

    if payload.max_uses is not None:
        updates['max_uses'] = 999999 if payload.max_uses == 0 else payload.max_uses

    if payload.valid_from is not None:
        updates['valid_from'] = _normalize_datetime(payload.valid_from)

    if payload.valid_until is not None:
        updates['valid_until'] = _normalize_datetime(payload.valid_until)

    if payload.is_active is not None:
        updates['is_active'] = payload.is_active

    if not updates:
        return _serialize_promocode(promocode)

    promocode = await update_promocode(db, promocode, **updates)
    return _serialize_promocode(promocode)


@router.delete(
    '/{promocode_id}',
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_promocode_endpoint(
    promocode_id: int,
    _: Any = Depends(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    promocode = await get_promocode_by_id(db, promocode_id)
    if not promocode:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_PROMOCODES_NOT_FOUND', 'Promo code not found'),
        )

    success = await delete_promocode(db, promocode)
    if not success:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=get_texts('ru').t('WEBAPI_PROMOCODES_FAILED_TO_DELETE', 'Failed to delete promo code'),
        )

    return Response(status_code=status.HTTP_204_NO_CONTENT)
