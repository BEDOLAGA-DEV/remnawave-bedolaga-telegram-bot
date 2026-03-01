"""Admin promocodes routes for cabinet."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.promo_group import (
    count_promo_group_members,
    count_promo_groups,
    create_promo_group,
    delete_promo_group,
    get_promo_group_by_id,
    get_promo_groups_with_counts,
    update_promo_group,
)
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
from app.database.models import PromoCode, PromoCodeType, PromoCodeUse, PromoGroup, User

from ..dependencies import get_cabinet_db, require_permission


router = APIRouter(prefix='/admin/promocodes', tags=['Admin Promocodes'])


# ============== Schemas ==============


class PromoCodeResponse(BaseModel):
    id: int
    code: str
    type: PromoCodeType
    balance_bonus_kopeks: int
    balance_bonus_rubles: float
    subscription_days: int
    max_uses: int
    current_uses: int
    uses_left: int
    is_active: bool
    is_valid: bool
    first_purchase_only: bool
    valid_from: datetime
    valid_until: datetime | None = None
    promo_group_id: int | None = None
    created_by: int | None = None
    created_at: datetime
    updated_at: datetime


class PromoCodeListResponse(BaseModel):
    items: list[PromoCodeResponse]
    total: int
    limit: int
    offset: int


class PromoCodeRecentUse(BaseModel):
    id: int
    user_id: int
    user_username: str | None = None
    user_full_name: str | None = None
    user_telegram_id: int | None = None
    used_at: datetime


class PromoCodeDetailResponse(PromoCodeResponse):
    total_uses: int
    today_uses: int
    recent_uses: list[PromoCodeRecentUse] = Field(default_factory=list)


class PromoCodeCreateRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=50)
    type: PromoCodeType
    balance_bonus_kopeks: int = 0
    subscription_days: int = 0
    max_uses: int = Field(default=1, ge=0)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    is_active: bool = True
    first_purchase_only: bool = False
    promo_group_id: int | None = None


class PromoCodeUpdateRequest(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=50)
    type: PromoCodeType | None = None
    balance_bonus_kopeks: int | None = None
    subscription_days: int | None = None
    max_uses: int | None = Field(default=None, ge=0)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    is_active: bool | None = None
    first_purchase_only: bool | None = None
    promo_group_id: int | None = None


# ============== PromoGroup Schemas ==============


class PromoGroupResponse(BaseModel):
    id: int
    name: str
    server_discount_percent: int
    traffic_discount_percent: int
    device_discount_percent: int
    period_discounts: dict[int, int] = Field(default_factory=dict)
    auto_assign_total_spent_kopeks: int | None = None
    apply_discounts_to_addons: bool
    is_default: bool
    members_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PromoGroupListResponse(BaseModel):
    items: list[PromoGroupResponse]
    total: int
    limit: int
    offset: int


class PromoGroupCreateRequest(BaseModel):
    name: str
    server_discount_percent: int = 0
    traffic_discount_percent: int = 0
    device_discount_percent: int = 0
    period_discounts: dict[int, int] | None = None
    auto_assign_total_spent_kopeks: int | None = None
    apply_discounts_to_addons: bool = True
    is_default: bool = False


class PromoGroupUpdateRequest(BaseModel):
    name: str | None = None
    server_discount_percent: int | None = None
    traffic_discount_percent: int | None = None
    device_discount_percent: int | None = None
    period_discounts: dict[int, int] | None = None
    auto_assign_total_spent_kopeks: int | None = None
    apply_discounts_to_addons: bool | None = None
    is_default: bool | None = None


# ============== Helpers ==============


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
        first_purchase_only=promocode.first_purchase_only,
        valid_from=promocode.valid_from,
        valid_until=promocode.valid_until,
        promo_group_id=promocode.promo_group_id,
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


def _normalize_period_discounts(group: PromoGroup) -> dict[int, int]:
    raw = group.period_discounts or {}
    normalized: dict[int, int] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            try:
                normalized[int(key)] = int(value)
            except (TypeError, ValueError):
                continue
    return normalized


def _serialize_promo_group(group: PromoGroup, members_count: int = 0) -> PromoGroupResponse:
    return PromoGroupResponse(
        id=group.id,
        name=group.name,
        server_discount_percent=group.server_discount_percent,
        traffic_discount_percent=group.traffic_discount_percent,
        device_discount_percent=group.device_discount_percent,
        period_discounts=_normalize_period_discounts(group),
        auto_assign_total_spent_kopeks=group.auto_assign_total_spent_kopeks,
        apply_discounts_to_addons=group.apply_discounts_to_addons,
        is_default=group.is_default,
        members_count=members_count,
        created_at=getattr(group, 'created_at', None),
        updated_at=getattr(group, 'updated_at', None),
    )


def _validate_create_payload(payload: PromoCodeCreateRequest) -> None:
    code = payload.code.strip()
    if not code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Code must not be empty')

    normalized_valid_from = _normalize_datetime(payload.valid_from)
    normalized_valid_until = _normalize_datetime(payload.valid_until)

    if payload.type == PromoCodeType.BALANCE and payload.balance_bonus_kopeks <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Balance bonus must be positive for balance promo codes')

    if payload.type in {PromoCodeType.SUBSCRIPTION_DAYS, PromoCodeType.TRIAL_SUBSCRIPTION}:
        if payload.subscription_days <= 0:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, 'Subscription days must be positive for this promo code type'
            )

    if payload.type == PromoCodeType.DISCOUNT:
        if payload.balance_bonus_kopeks <= 0 or payload.balance_bonus_kopeks > 100:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Discount percent must be between 1 and 100')
        if payload.subscription_days <= 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Discount validity hours must be positive')

    if normalized_valid_from and normalized_valid_until and normalized_valid_from > normalized_valid_until:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'valid_from cannot be greater than valid_until')


def _validate_update_payload(payload: PromoCodeUpdateRequest, promocode: PromoCode) -> None:
    if payload.code is not None and not payload.code.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Code must not be empty')

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
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Balance bonus must be positive for balance promo codes')

    if new_type in {PromoCodeType.SUBSCRIPTION_DAYS, PromoCodeType.TRIAL_SUBSCRIPTION}:
        if subscription_days <= 0:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, 'Subscription days must be positive for this promo code type'
            )

    if new_type == PromoCodeType.DISCOUNT:
        if balance_bonus <= 0 or balance_bonus > 100:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Discount percent must be between 1 and 100')
        if subscription_days <= 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Discount validity hours must be positive')

    valid_from = _normalize_datetime(payload.valid_from) if payload.valid_from is not None else promocode.valid_from
    valid_until = _normalize_datetime(payload.valid_until) if payload.valid_until is not None else promocode.valid_until

    if valid_from and valid_until and valid_from > valid_until:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'valid_from cannot be greater than valid_until')

    if payload.max_uses is not None and payload.max_uses != 0 and payload.max_uses < promocode.current_uses:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'max_uses cannot be less than current uses')


# ============== Promocode Endpoints ==============


@router.get('', response_model=PromoCodeListResponse)
async def list_promocodes(
    admin: User = Depends(require_permission('promocodes:read')),
    db: AsyncSession = Depends(get_cabinet_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    is_active: bool | None = Query(default=None),
) -> PromoCodeListResponse:
    """Get list of all promocodes."""
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
    admin: User = Depends(require_permission('promocodes:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PromoCodeDetailResponse:
    """Get promocode details with usage statistics."""
    promocode = await get_promocode_by_id(db, promocode_id)
    if not promocode:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Promo code not found')

    stats = await get_promocode_statistics(db, promocode_id)
    base = _serialize_promocode(promocode)
    recent_uses = [_serialize_recent_use(use) for use in stats.get('recent_uses', [])]

    return PromoCodeDetailResponse(
        **base.model_dump(),
        total_uses=stats.get('total_uses', 0),
        today_uses=stats.get('today_uses', 0),
        recent_uses=recent_uses,
    )


@router.post('', response_model=PromoCodeResponse, status_code=status.HTTP_201_CREATED)
async def create_promocode_endpoint(
    payload: PromoCodeCreateRequest,
    admin: User = Depends(require_permission('promocodes:create')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PromoCodeResponse:
    """Create a new promocode."""
    _validate_create_payload(payload)

    normalized_code = payload.code.strip().upper()
    normalized_valid_from = _normalize_datetime(payload.valid_from)
    normalized_valid_until = _normalize_datetime(payload.valid_until)

    existing = await get_promocode_by_code(db, normalized_code)
    if existing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Promo code with this code already exists')

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
        created_by=admin.id,
    )

    update_fields = {}
    if normalized_valid_from is not None:
        update_fields['valid_from'] = normalized_valid_from
    if payload.is_active is not None and payload.is_active != promocode.is_active:
        update_fields['is_active'] = payload.is_active
    if normalized_valid_until is not None:
        update_fields['valid_until'] = normalized_valid_until
    if payload.first_purchase_only:
        update_fields['first_purchase_only'] = payload.first_purchase_only
    if payload.promo_group_id is not None:
        update_fields['promo_group_id'] = payload.promo_group_id

    if update_fields:
        promocode = await update_promocode(db, promocode, **update_fields)

    return _serialize_promocode(promocode)


@router.patch('/{promocode_id}', response_model=PromoCodeResponse)
async def update_promocode_endpoint(
    promocode_id: int,
    payload: PromoCodeUpdateRequest,
    admin: User = Depends(require_permission('promocodes:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PromoCodeResponse:
    """Update an existing promocode."""
    promocode = await get_promocode_by_id(db, promocode_id)
    if not promocode:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Promo code not found')

    _validate_update_payload(payload, promocode)

    updates: dict[str, Any] = {}

    if payload.code is not None:
        normalized_code = payload.code.strip().upper()
        if normalized_code != promocode.code:
            existing = await get_promocode_by_code(db, normalized_code)
            if existing and existing.id != promocode_id:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Promo code with this code already exists')
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

    if payload.first_purchase_only is not None:
        updates['first_purchase_only'] = payload.first_purchase_only

    if payload.promo_group_id is not None:
        updates['promo_group_id'] = payload.promo_group_id

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
    admin: User = Depends(require_permission('promocodes:delete')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> Response:
    """Delete a promocode."""
    promocode = await get_promocode_by_id(db, promocode_id)
    if not promocode:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Promo code not found')

    success = await delete_promocode(db, promocode)
    if not success:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Failed to delete promo code')

    return Response(status_code=status.HTTP_204_NO_CONTENT)


class DeactivateDiscountResponse(BaseModel):
    success: bool
    message: str
    deactivated_code: str | None = None
    discount_percent: int = 0
    user_id: int


@router.post('/deactivate-discount/{user_id}', response_model=DeactivateDiscountResponse)
async def admin_deactivate_discount_promocode(
    user_id: int,
    admin: User = Depends(require_permission('promocodes:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> DeactivateDiscountResponse:
    """Admin: deactivate a user's active discount promo code."""
    from app.database.crud.user import get_user_by_id as get_user

    target_user = await get_user(db, user_id)
    if not target_user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'User not found')

    from app.services.promocode_service import PromoCodeService

    service = PromoCodeService()
    result = await service.deactivate_discount_promocode(
        db=db,
        user_id=user_id,
        admin_initiated=True,
    )

    if result['success']:
        return DeactivateDiscountResponse(
            success=True,
            message=f'Discount promo code deactivated for user {user_id}',
            deactivated_code=result.get('deactivated_code'),
            discount_percent=result.get('discount_percent', 0),
            user_id=user_id,
        )

    error_messages = {
        'user_not_found': 'User not found',
        'no_active_discount_promocode': 'User has no active discount from a promo code',
        'discount_already_expired': 'Discount has already expired (cleaned up)',
        'server_error': 'Server error occurred',
    }

    error_code = result.get('error', 'server_error')
    error_message = error_messages.get(error_code, 'Failed to deactivate promo code')

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=error_message,
    )


# ============== PromoGroup Endpoints ==============

promo_groups_router = APIRouter(prefix='/admin/promo-groups', tags=['Admin Promo Groups'])


@promo_groups_router.get('', response_model=PromoGroupListResponse)
async def list_promo_groups(
    admin: User = Depends(require_permission('promo_groups:read')),
    db: AsyncSession = Depends(get_cabinet_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> PromoGroupListResponse:
    """Get list of all promo groups."""
    total = await count_promo_groups(db)
    groups_with_counts = await get_promo_groups_with_counts(
        db,
        offset=offset,
        limit=limit,
    )

    return PromoGroupListResponse(
        items=[_serialize_promo_group(group, members_count=count) for group, count in groups_with_counts],
        total=total,
        limit=limit,
        offset=offset,
    )


@promo_groups_router.get('/{group_id}', response_model=PromoGroupResponse)
async def get_promo_group(
    group_id: int,
    admin: User = Depends(require_permission('promo_groups:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PromoGroupResponse:
    """Get promo group details."""
    group = await get_promo_group_by_id(db, group_id)
    if not group:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Promo group not found')

    members_count = await count_promo_group_members(db, group_id)
    return _serialize_promo_group(group, members_count=members_count)


@promo_groups_router.post('', response_model=PromoGroupResponse, status_code=status.HTTP_201_CREATED)
async def create_promo_group_endpoint(
    payload: PromoGroupCreateRequest,
    admin: User = Depends(require_permission('promo_groups:create')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PromoGroupResponse:
    """Create a new promo group."""
    from sqlalchemy.exc import IntegrityError

    try:
        group = await create_promo_group(
            db,
            name=payload.name,
            server_discount_percent=payload.server_discount_percent,
            traffic_discount_percent=payload.traffic_discount_percent,
            device_discount_percent=payload.device_discount_percent,
            period_discounts=payload.period_discounts,
            auto_assign_total_spent_kopeks=payload.auto_assign_total_spent_kopeks,
            apply_discounts_to_addons=payload.apply_discounts_to_addons,
            is_default=payload.is_default,
        )
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            'Promo group with this name already exists',
        )

    return _serialize_promo_group(group, members_count=0)


@promo_groups_router.patch('/{group_id}', response_model=PromoGroupResponse)
async def update_promo_group_endpoint(
    group_id: int,
    payload: PromoGroupUpdateRequest,
    admin: User = Depends(require_permission('promo_groups:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PromoGroupResponse:
    """Update a promo group."""
    from sqlalchemy.exc import IntegrityError

    group = await get_promo_group_by_id(db, group_id)
    if not group:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Promo group not found')

    try:
        group = await update_promo_group(
            db,
            group,
            name=payload.name,
            server_discount_percent=payload.server_discount_percent,
            traffic_discount_percent=payload.traffic_discount_percent,
            device_discount_percent=payload.device_discount_percent,
            period_discounts=payload.period_discounts,
            auto_assign_total_spent_kopeks=payload.auto_assign_total_spent_kopeks,
            apply_discounts_to_addons=payload.apply_discounts_to_addons,
            is_default=payload.is_default,
        )
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            'Promo group with this name already exists',
        )

    members_count = await count_promo_group_members(db, group_id)
    return _serialize_promo_group(group, members_count=members_count)


@promo_groups_router.delete('/{group_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_promo_group_endpoint(
    group_id: int,
    admin: User = Depends(require_permission('promo_groups:delete')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> Response:
    """Delete a promo group."""
    group = await get_promo_group_by_id(db, group_id)
    if not group:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Promo group not found')

    success = await delete_promo_group(db, group)
    if not success:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Cannot delete default promo group')

    return Response(status_code=status.HTTP_204_NO_CONTENT)
