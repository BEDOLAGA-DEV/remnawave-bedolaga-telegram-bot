"""Admin achievements routes for cabinet."""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.achievement import (
    count_total_unlocks,
    count_unique_users_with_achievements,
    count_unlocks_per_template,
    create_template as crud_create_template,
    delete_template as crud_delete_template,
    get_all_templates,
    get_template_by_id,
    update_template as crud_update_template,
)
from app.database.models import AchievementTemplate, User

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/achievements', tags=['Admin Achievements'])


# Allowed values for conditions and rewards (matches bot logic)
CONDITION_TYPES = {
    'total_spent_kopeks',
    'days_active',
    'referral_count',
    'traffic_gb',
    'topup_count',
    'review_left',
}

REWARD_TYPES = {
    'balance_kopeks',
    'traffic_gb',
    'subscription_days',
    'none',
}


# ============== Schemas ==============


class AchievementTemplateResponse(BaseModel):
    id: int
    name: str
    description: str | None = None
    emoji: str
    condition_type: str
    condition_value: int
    reward_type: str
    reward_value: int
    reward_duration_days: int | None = None
    is_active: bool
    display_order: int
    created_at: datetime | None = None
    unlock_count: int = 0


class AchievementTemplateListResponse(BaseModel):
    items: list[AchievementTemplateResponse]
    total: int


class AchievementTemplateCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    emoji: str = Field(default='🏆', max_length=10)
    condition_type: str = Field(..., min_length=1, max_length=50)
    condition_value: int = Field(..., ge=0)
    reward_type: str = Field(..., min_length=1, max_length=50)
    reward_value: int = Field(default=0, ge=0)
    reward_duration_days: int | None = Field(default=None, ge=0)
    is_active: bool = True
    display_order: int = 0


class AchievementTemplateUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    emoji: str | None = Field(default=None, max_length=10)
    condition_type: str | None = Field(default=None, min_length=1, max_length=50)
    condition_value: int | None = Field(default=None, ge=0)
    reward_type: str | None = Field(default=None, min_length=1, max_length=50)
    reward_value: int | None = Field(default=None, ge=0)
    reward_duration_days: int | None = Field(default=None, ge=0)
    is_active: bool | None = None
    display_order: int | None = None


class AchievementStatsResponse(BaseModel):
    total_templates: int
    active_templates: int
    total_unlocks: int
    unique_users_unlocked: int
    most_popular: AchievementTemplateResponse | None = None


# ============== Helpers ==============


def _serialize_template(
    template: AchievementTemplate,
    unlock_count: int = 0,
) -> AchievementTemplateResponse:
    return AchievementTemplateResponse(
        id=template.id,
        name=template.name,
        description=template.description,
        emoji=template.emoji or '🏆',
        condition_type=template.condition_type,
        condition_value=template.condition_value,
        reward_type=template.reward_type,
        reward_value=template.reward_value or 0,
        reward_duration_days=template.reward_duration_days,
        is_active=bool(template.is_active),
        display_order=template.display_order or 0,
        created_at=template.created_at,
        unlock_count=unlock_count,
    )


def _validate_condition_type(condition_type: str) -> None:
    if condition_type not in CONDITION_TYPES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f'Invalid condition_type: {condition_type}. Must be one of {sorted(CONDITION_TYPES)}',
        )


def _validate_reward_type(reward_type: str) -> None:
    if reward_type not in REWARD_TYPES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f'Invalid reward_type: {reward_type}. Must be one of {sorted(REWARD_TYPES)}',
        )


# ============== Endpoints ==============


@router.get('/templates', response_model=AchievementTemplateListResponse)
async def list_templates(
    admin: User = Depends(require_permission('achievements:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AchievementTemplateListResponse:
    """List all achievement templates with unlock counts."""
    templates = await get_all_templates(db)
    unlock_map = await count_unlocks_per_template(db)

    items = [
        _serialize_template(t, unlock_count=unlock_map.get(t.id, 0))
        for t in templates
    ]
    return AchievementTemplateListResponse(items=items, total=len(items))


@router.get('/stats', response_model=AchievementStatsResponse)
async def get_stats(
    admin: User = Depends(require_permission('achievements:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AchievementStatsResponse:
    """Overall achievement statistics."""
    templates = await get_all_templates(db)
    unlock_map = await count_unlocks_per_template(db)
    total_unlocks = await count_total_unlocks(db)
    unique_users = await count_unique_users_with_achievements(db)

    active_count = sum(1 for t in templates if t.is_active)

    most_popular: AchievementTemplateResponse | None = None
    if unlock_map and templates:
        by_id = {t.id: t for t in templates}
        top_tid, top_count = max(unlock_map.items(), key=lambda kv: kv[1])
        top_template = by_id.get(top_tid)
        if top_template:
            most_popular = _serialize_template(top_template, unlock_count=top_count)

    return AchievementStatsResponse(
        total_templates=len(templates),
        active_templates=active_count,
        total_unlocks=total_unlocks,
        unique_users_unlocked=unique_users,
        most_popular=most_popular,
    )


@router.get('/templates/{template_id}', response_model=AchievementTemplateResponse)
async def get_template(
    template_id: int,
    admin: User = Depends(require_permission('achievements:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AchievementTemplateResponse:
    """Get a single achievement template."""
    template = await get_template_by_id(db, template_id)
    if not template:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Template not found')
    unlock_map = await count_unlocks_per_template(db)
    return _serialize_template(template, unlock_count=unlock_map.get(template.id, 0))


@router.post(
    '/templates',
    response_model=AchievementTemplateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_template(
    payload: AchievementTemplateCreateRequest,
    admin: User = Depends(require_permission('achievements:create')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AchievementTemplateResponse:
    """Create a new achievement template."""
    _validate_condition_type(payload.condition_type)
    _validate_reward_type(payload.reward_type)

    template = await crud_create_template(
        db,
        name=payload.name,
        description=payload.description,
        emoji=payload.emoji or '🏆',
        condition_type=payload.condition_type,
        condition_value=payload.condition_value,
        reward_type=payload.reward_type,
        reward_value=payload.reward_value,
        reward_duration_days=payload.reward_duration_days,
        is_active=payload.is_active,
        display_order=payload.display_order,
    )
    await db.commit()
    await db.refresh(template)
    logger.info(
        'Achievement template created',
        template_id=template.id,
        admin_id=admin.id,
    )
    return _serialize_template(template)


@router.patch('/templates/{template_id}', response_model=AchievementTemplateResponse)
async def update_template(
    template_id: int,
    payload: AchievementTemplateUpdateRequest,
    admin: User = Depends(require_permission('achievements:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AchievementTemplateResponse:
    """Update an achievement template."""
    if payload.condition_type is not None:
        _validate_condition_type(payload.condition_type)
    if payload.reward_type is not None:
        _validate_reward_type(payload.reward_type)

    fields_set = payload.model_fields_set
    description_set = 'description' in fields_set
    reward_duration_set = 'reward_duration_days' in fields_set

    template = await crud_update_template(
        db,
        template_id,
        name=payload.name,
        description=payload.description,
        emoji=payload.emoji,
        condition_type=payload.condition_type,
        condition_value=payload.condition_value,
        reward_type=payload.reward_type,
        reward_value=payload.reward_value,
        reward_duration_days=payload.reward_duration_days,
        is_active=payload.is_active,
        display_order=payload.display_order,
        _description_set=description_set,
        _reward_duration_set=reward_duration_set,
    )
    if not template:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Template not found')
    await db.commit()
    await db.refresh(template)
    logger.info(
        'Achievement template updated',
        template_id=template.id,
        admin_id=admin.id,
    )
    unlock_map = await count_unlocks_per_template(db)
    return _serialize_template(template, unlock_count=unlock_map.get(template.id, 0))


@router.delete('/templates/{template_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    template_id: int,
    admin: User = Depends(require_permission('achievements:delete')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> None:
    """Delete an achievement template."""
    deleted = await crud_delete_template(db, template_id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Template not found')
    await db.commit()
    logger.info(
        'Achievement template deleted',
        template_id=template_id,
        admin_id=admin.id,
    )
