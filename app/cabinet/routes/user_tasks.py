"""User-side endpoints для системы заданий с наградами."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.cabinet.schemas.tasks import (
    ClaimRewardRequest,
    ClaimRewardResponse,
    UserTaskProgressResponse,
    UserTasksAvailabilityResponse,
    UserTasksListResponse,
)
from app.database.models import User
from app.services import tasks_service


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/tasks', tags=['Cabinet Tasks'])


@router.get('/availability', response_model=UserTasksAvailabilityResponse)
async def get_tasks_availability(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Краткая информация для условного показа вкладки «Задания» в меню."""
    visible = await tasks_service.get_available_tasks_for_user(db, user)
    has_available = len(visible) > 0
    unclaimed = await tasks_service.count_completed_unclaimed(db, user_id=user.id)
    return UserTasksAvailabilityResponse(
        has_available_tasks=has_available,
        unclaimed_count=unclaimed,
    )


@router.get('', response_model=UserTasksListResponse)
async def list_my_tasks(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Список доступных заданий пользователя с их прогрессом."""
    visible = await tasks_service.get_available_tasks_for_user(db, user)

    items: list[UserTaskProgressResponse] = []
    unclaimed_count = 0

    for task, progress in visible:
        current_value = progress.current_value if progress else 0
        is_completed = progress.completed_at is not None if progress else False
        is_claimed = progress.claimed_at is not None if progress else False
        if is_completed and not is_claimed:
            unclaimed_count += 1
        percent = (
            int(min(current_value, task.target_value) / max(task.target_value, 1) * 100)
            if task.target_value
            else 0
        )
        items.append(
            UserTaskProgressResponse(
                task_id=task.id,
                title=task.title or {},
                description=task.description or {},
                icon=task.icon,
                task_type=task.task_type,
                target_value=task.target_value,
                target_meta=task.target_meta or {},
                reward_type=task.reward_type,
                reward_value=task.reward_value,
                reward_meta=task.reward_meta or {},
                allow_user_choice=task.allow_user_choice,
                level=task.level,
                parent_task_id=task.parent_task_id,
                current_value=current_value,
                percent=percent,
                is_completed=is_completed,
                is_claimed=is_claimed,
                completed_at=progress.completed_at if progress else None,
                claimed_at=progress.claimed_at if progress else None,
                reward_granted_meta=progress.reward_granted_meta if progress else None,
            )
        )

    return UserTasksListResponse(
        items=items,
        has_unclaimed=unclaimed_count > 0,
        unclaimed_count=unclaimed_count,
    )


@router.post('/{task_id}/claim', response_model=ClaimRewardResponse)
async def claim_task_reward(
    task_id: int,
    request: ClaimRewardRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Получить награду за выполненное задание."""
    try:
        granted = await tasks_service.claim_reward(
            db,
            user_id=user.id,
            task_id=task_id,
            chosen_subscription_id=request.chosen_subscription_id,
            chosen_reward_type=request.chosen_reward_type,
        )
    except ValueError as exc:
        msg = str(exc)
        # Маппим внутренние коды на HTTP-статусы
        not_found = {'progress_not_found', 'task_not_found', 'user_not_found'}
        bad_request = {
            'not_completed',
            'already_claimed',
            'user_not_eligible',
            'user_choice_not_allowed',
            'no_paid_subscription',
            'no_subscription_with_target_tariff',
            'chosen_subscription_invalid',
            'need_choose_subscription',
            'invalid_reward_amount',
            'invalid_reward_days',
        }
        if msg in not_found:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg) from exc
        if msg in bad_request or msg.startswith('unknown_reward_type'):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg) from exc
        logger.exception('claim_reward unexpected error', error=msg)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail='internal_error'
        ) from exc

    return ClaimRewardResponse(success=True, reward=granted)
