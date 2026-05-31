"""Admin routes for managing partner promos."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud import partner_promo as crud
from app.database.models import User

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.partner_promo import (
    PartnerPromoCreateRequest,
    PartnerPromoResponse,
    PartnerPromoUpdateRequest,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/partner-promos', tags=['Cabinet Admin Partner Promos'])


@router.get('', response_model=list[PartnerPromoResponse])
async def list_all_partner_promos(
    admin: User = Depends(require_permission('partners:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> list[PartnerPromoResponse]:
    """Get all partner promos (admin view, includes inactive)."""
    try:
        promos = await crud.list_all(db)
        return [PartnerPromoResponse.model_validate(p) for p in promos]
    except HTTPException:
        raise
    except Exception:
        logger.exception('Failed to list partner promos')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load partner promos',
        )


@router.get('/{promo_id}', response_model=PartnerPromoResponse)
async def get_partner_promo(
    promo_id: int,
    admin: User = Depends(require_permission('partners:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PartnerPromoResponse:
    """Get a single partner promo by ID (admin view)."""
    promo = await crud.get(db, promo_id)
    if not promo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Partner promo not found',
        )
    return PartnerPromoResponse.model_validate(promo)


@router.post('', response_model=PartnerPromoResponse, status_code=status.HTTP_201_CREATED)
async def create_partner_promo(
    request: PartnerPromoCreateRequest,
    admin: User = Depends(require_permission('partners:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PartnerPromoResponse:
    """Create a new partner promo."""
    try:
        promo = await crud.create(
            db,
            title=request.title,
            url=request.url,
            description=request.description,
            image_url=request.image_url,
            is_active=request.is_active,
            sort_order=request.sort_order,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={'code': 'invalid_url', 'message': str(exc)},
        )
    except Exception:
        logger.exception('Failed to create partner promo')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to create partner promo',
        )
    return PartnerPromoResponse.model_validate(promo)


@router.put('/{promo_id}', response_model=PartnerPromoResponse)
async def update_partner_promo(
    promo_id: int,
    request: PartnerPromoUpdateRequest,
    admin: User = Depends(require_permission('partners:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PartnerPromoResponse:
    """Update an existing partner promo."""
    existing = await crud.get(db, promo_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Partner promo not found',
        )

    try:
        update_data = request.model_dump(exclude_unset=True)
        promo = await crud.update_promo(db, promo_id, **update_data)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={'code': 'invalid_url', 'message': str(exc)},
        )
    except Exception:
        logger.exception('Failed to update partner promo', promo_id=promo_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to update partner promo',
        )

    if not promo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Partner promo not found after update',
        )
    return PartnerPromoResponse.model_validate(promo)


@router.delete('/{promo_id}', status_code=status.HTTP_204_NO_CONTENT)
async def remove_partner_promo(
    promo_id: int,
    admin: User = Depends(require_permission('partners:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> None:
    """Delete a partner promo."""
    existing = await crud.get(db, promo_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Partner promo not found',
        )

    try:
        await crud.delete(db, promo_id)
    except Exception:
        logger.exception('Failed to delete partner promo', promo_id=promo_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to delete partner promo',
        )


@router.post('/{promo_id}/toggle-active', response_model=PartnerPromoResponse)
async def toggle_active(
    promo_id: int,
    admin: User = Depends(require_permission('partners:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PartnerPromoResponse:
    """Toggle the active status of a partner promo."""
    existing = await crud.get(db, promo_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Partner promo not found',
        )

    try:
        promo = await crud.update_promo(db, promo_id, is_active=not existing.is_active)
    except Exception:
        logger.exception('Failed to toggle partner promo active status', promo_id=promo_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to toggle active status',
        )

    if not promo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Partner promo not found after toggle',
        )
    return PartnerPromoResponse.model_validate(promo)
