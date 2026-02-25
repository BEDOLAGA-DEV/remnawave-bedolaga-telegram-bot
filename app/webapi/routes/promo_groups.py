from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, Security, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.localization.texts import get_texts
from app.database.crud.promo_group import (
    count_promo_group_members,
    count_promo_groups,
    create_promo_group,
    delete_promo_group,
    get_promo_group_by_id,
    get_promo_groups_with_counts,
    update_promo_group,
)
from app.database.models import PromoGroup

from ..dependencies import get_db_session, require_api_token
from ..schemas.promo_groups import (
    PromoGroupCreateRequest,
    PromoGroupListResponse,
    PromoGroupResponse,
    PromoGroupUpdateRequest,
)


router = APIRouter()


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


def _serialize(group: PromoGroup, members_count: int = 0) -> PromoGroupResponse:
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


@router.get('', response_model=PromoGroupListResponse, response_model_exclude_none=True)
async def list_promo_groups(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> PromoGroupListResponse:
    total = await count_promo_groups(db)
    groups_with_counts = await get_promo_groups_with_counts(
        db,
        offset=offset,
        limit=limit,
    )

    return PromoGroupListResponse(
        items=[_serialize(group, members_count=count) for group, count in groups_with_counts],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get('/{group_id}', response_model=PromoGroupResponse, response_model_exclude_none=True)
async def get_promo_group(
    group_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PromoGroupResponse:
    group = await get_promo_group_by_id(db, group_id)
    if not group:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_PROMO_GROUPS_NOT_FOUND', 'Promo group not found'),
        )

    members_count = await count_promo_group_members(db, group_id)
    return _serialize(group, members_count=members_count)


@router.post(
    '',
    response_model=PromoGroupResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
)
async def create_promo_group_endpoint(
    payload: PromoGroupCreateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PromoGroupResponse:
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
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=get_texts('ru').t(
                'WEBAPI_PROMO_GROUPS_NAME_ALREADY_EXISTS',
                'Promo group with this name already exists',
            ),
        ) from exc
    return _serialize(group, members_count=0)


@router.patch(
    '/{group_id}',
    response_model=PromoGroupResponse,
    response_model_exclude_none=True,
)
async def update_promo_group_endpoint(
    group_id: int,
    payload: PromoGroupUpdateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PromoGroupResponse:
    group = await get_promo_group_by_id(db, group_id)
    if not group:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_PROMO_GROUPS_NOT_FOUND', 'Promo group not found'),
        )

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
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=get_texts('ru').t(
                'WEBAPI_PROMO_GROUPS_NAME_ALREADY_EXISTS',
                'Promo group with this name already exists',
            ),
        ) from exc
    members_count = await count_promo_group_members(db, group_id)
    return _serialize(group, members_count=members_count)


@router.delete('/{group_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_promo_group_endpoint(
    group_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    group = await get_promo_group_by_id(db, group_id)
    if not group:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_PROMO_GROUPS_NOT_FOUND', 'Promo group not found'),
        )

    success = await delete_promo_group(db, group)
    if not success:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=get_texts('ru').t(
                'WEBAPI_PROMO_GROUPS_CANNOT_DELETE_DEFAULT',
                'Cannot delete default promo group',
            ),
        )

    return Response(status_code=status.HTTP_204_NO_CONTENT)
