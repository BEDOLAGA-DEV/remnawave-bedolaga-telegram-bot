"""Admin endpoints для системы заданий с наградами и партнёрских каналов."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.dependencies import get_cabinet_db, require_permission
from app.cabinet.schemas.tasks import (
    TaskCreateRequest,
    TaskListItem,
    TaskPartnerChannelCreateRequest,
    TaskPartnerChannelResponse,
    TaskPartnerChannelUpdateRequest,
    TaskResponse,
    TaskUpdateRequest,
)
from app.database.crud import tasks as tasks_crud
from app.database.models import User


router = APIRouter(prefix='/admin', tags=['Cabinet Admin Tasks'])


# ===========================================================================
# Tasks
# ===========================================================================


@router.get('/tasks', response_model=list[TaskListItem])
async def admin_list_tasks(
    include_inactive: bool = True,
    parent_task_id: int | None = None,
    admin: User = Depends(require_permission('tasks:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    tasks = await tasks_crud.list_tasks(db, include_inactive=include_inactive, parent_task_id=parent_task_id)
    return [TaskListItem.model_validate(t) for t in tasks]


@router.get('/tasks/{task_id}', response_model=TaskResponse)
async def admin_get_task(
    task_id: int,
    admin: User = Depends(require_permission('tasks:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    task = await tasks_crud.get_task_by_id(db, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='task_not_found')
    return TaskResponse.model_validate(task)


@router.post('/tasks', response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_task(
    request: TaskCreateRequest,
    admin: User = Depends(require_permission('tasks:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    if request.parent_task_id is not None:
        parent = await tasks_crud.get_task_by_id(db, request.parent_task_id)
        if parent is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='parent_task_not_found')

    task = await tasks_crud.create_task(
        db,
        title=request.title,
        description=request.description,
        task_type=request.task_type,
        reward_type=request.reward_type,
        target_value=request.target_value,
        reward_value=request.reward_value,
        target_meta=request.target_meta,
        reward_meta=request.reward_meta,
        icon=request.icon,
        is_active=request.is_active,
        sort_order=request.sort_order,
        allow_user_choice=request.allow_user_choice,
        user_audience=request.user_audience,
        promo_group_id=request.promo_group_id,
        parent_task_id=request.parent_task_id,
        level=request.level,
        starts_at=request.starts_at,
        ends_at=request.ends_at,
    )
    return TaskResponse.model_validate(task)


async def _parent_chain_has_cycle(db: AsyncSession, *, task_id: int, parent_id: int, max_depth: int = 10) -> bool:
    """Идёт вверх по цепочке parent — проверяет, не возвращается ли в task_id."""
    current = parent_id
    visited: set[int] = set()
    for _ in range(max_depth):
        if current == task_id:
            return True
        if current in visited:
            return False
        visited.add(current)
        parent = await tasks_crud.get_task_by_id(db, current)
        if parent is None or parent.parent_task_id is None:
            return False
        current = parent.parent_task_id
    return False  # max_depth достигнут — дальше не считаем циклом


@router.put('/tasks/{task_id}', response_model=TaskResponse)
async def admin_update_task(
    task_id: int,
    request: TaskUpdateRequest,
    admin: User = Depends(require_permission('tasks:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    task = await tasks_crud.get_task_by_id(db, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='task_not_found')

    if request.parent_task_id is not None and request.parent_task_id == task_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='parent_task_cannot_be_self')

    if request.parent_task_id is not None:
        if await _parent_chain_has_cycle(db, task_id=task_id, parent_id=request.parent_task_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='parent_chain_cycle_detected')

    fields = request.model_dump(exclude_unset=True)
    if not fields:
        return TaskResponse.model_validate(task)

    updated = await tasks_crud.update_task(db, task, **fields)
    return TaskResponse.model_validate(updated)


@router.delete('/tasks/{task_id}', status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_task(
    task_id: int,
    admin: User = Depends(require_permission('tasks:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    task = await tasks_crud.get_task_by_id(db, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='task_not_found')
    await tasks_crud.delete_task(db, task)


# ===========================================================================
# Partner channels
# ===========================================================================


@router.get('/task-partner-channels', response_model=list[TaskPartnerChannelResponse])
async def admin_list_partner_channels(
    include_inactive: bool = True,
    admin: User = Depends(require_permission('tasks:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    channels = await tasks_crud.list_partner_channels(db, include_inactive=include_inactive)
    return [TaskPartnerChannelResponse.model_validate(c) for c in channels]


@router.post(
    '/task-partner-channels',
    response_model=TaskPartnerChannelResponse,
    status_code=status.HTTP_201_CREATED,
)
async def admin_create_partner_channel(
    request: TaskPartnerChannelCreateRequest,
    admin: User = Depends(require_permission('tasks:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    existing = await tasks_crud.get_partner_channel_by_channel_id(db, request.channel_id)
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='channel_id_already_exists')
    channel = await tasks_crud.create_partner_channel(
        db,
        channel_id=request.channel_id,
        title=request.title,
        channel_link=request.channel_link,
        description=request.description,
        is_active=request.is_active,
        sort_order=request.sort_order,
    )
    return TaskPartnerChannelResponse.model_validate(channel)


@router.put('/task-partner-channels/{channel_pk}', response_model=TaskPartnerChannelResponse)
async def admin_update_partner_channel(
    channel_pk: int,
    request: TaskPartnerChannelUpdateRequest,
    admin: User = Depends(require_permission('tasks:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    channel = await tasks_crud.get_partner_channel_by_id(db, channel_pk)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='channel_not_found')
    fields = request.model_dump(exclude_unset=True)
    updated = await tasks_crud.update_partner_channel(db, channel, **fields)
    return TaskPartnerChannelResponse.model_validate(updated)


@router.delete('/task-partner-channels/{channel_pk}', status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_partner_channel(
    channel_pk: int,
    admin: User = Depends(require_permission('tasks:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    channel = await tasks_crud.get_partner_channel_by_id(db, channel_pk)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='channel_not_found')
    await tasks_crud.delete_partner_channel(db, channel)
