"""Admin routes for managing referral milestones."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud import referral_milestone as crud
from app.database.models import User

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.referral_milestone import (
    ReferralMilestoneCreateRequest,
    ReferralMilestoneResponse,
    ReferralMilestoneUpdateRequest,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/referral-milestones', tags=['Cabinet Admin Referral Milestones'])


@router.get('', response_model=list[ReferralMilestoneResponse])
async def list_all_milestones(
    admin: User = Depends(require_permission('referrals:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> list[ReferralMilestoneResponse]:
    """Get all referral milestones (admin view, includes inactive)."""
    try:
        milestones = await crud.list_all(db)
        return [ReferralMilestoneResponse.model_validate(m) for m in milestones]
    except HTTPException:
        raise
    except Exception:
        logger.exception('Failed to list referral milestones')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load referral milestones',
        )


@router.get('/{milestone_id}', response_model=ReferralMilestoneResponse)
async def get_milestone(
    milestone_id: int,
    admin: User = Depends(require_permission('referrals:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ReferralMilestoneResponse:
    """Get a single referral milestone by ID (admin view)."""
    milestone = await crud.get(db, milestone_id)
    if not milestone:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Referral milestone not found',
        )
    return ReferralMilestoneResponse.model_validate(milestone)


@router.post('', response_model=ReferralMilestoneResponse, status_code=status.HTTP_201_CREATED)
async def create_milestone(
    request: ReferralMilestoneCreateRequest,
    admin: User = Depends(require_permission('referrals:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ReferralMilestoneResponse:
    """Create a new referral milestone."""
    try:
        milestone = await crud.create(
            db,
            threshold=request.threshold,
            reward_type=request.reward_type,
            reward_value=request.reward_value,
            title=request.title,
            is_active=request.is_active,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={'code': 'invalid', 'message': str(exc)},
        )
    except Exception:
        logger.exception('Failed to create referral milestone')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to create referral milestone',
        )
    return ReferralMilestoneResponse.model_validate(milestone)


@router.put('/{milestone_id}', response_model=ReferralMilestoneResponse)
async def update_milestone(
    milestone_id: int,
    request: ReferralMilestoneUpdateRequest,
    admin: User = Depends(require_permission('referrals:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ReferralMilestoneResponse:
    """Update an existing referral milestone."""
    existing = await crud.get(db, milestone_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Referral milestone not found',
        )

    try:
        update_data = request.model_dump(exclude_unset=True)
        milestone = await crud.update_milestone(db, milestone_id, **update_data)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={'code': 'invalid', 'message': str(exc)},
        )
    except Exception:
        logger.exception('Failed to update referral milestone', milestone_id=milestone_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to update referral milestone',
        )

    if not milestone:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Referral milestone not found after update',
        )
    return ReferralMilestoneResponse.model_validate(milestone)


@router.delete('/{milestone_id}', status_code=status.HTTP_204_NO_CONTENT)
async def remove_milestone(
    milestone_id: int,
    admin: User = Depends(require_permission('referrals:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> None:
    """Delete a referral milestone."""
    existing = await crud.get(db, milestone_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Referral milestone not found',
        )

    try:
        await crud.delete(db, milestone_id)
    except Exception:
        logger.exception('Failed to delete referral milestone', milestone_id=milestone_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to delete referral milestone',
        )


@router.post('/{milestone_id}/toggle-active', response_model=ReferralMilestoneResponse)
async def toggle_active(
    milestone_id: int,
    admin: User = Depends(require_permission('referrals:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ReferralMilestoneResponse:
    """Toggle the active status of a referral milestone."""
    existing = await crud.get(db, milestone_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Referral milestone not found',
        )

    try:
        milestone = await crud.update_milestone(db, milestone_id, is_active=not existing.is_active)
    except Exception:
        logger.exception('Failed to toggle referral milestone active status', milestone_id=milestone_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to toggle active status',
        )

    if not milestone:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Referral milestone not found after toggle',
        )
    return ReferralMilestoneResponse.model_validate(milestone)
