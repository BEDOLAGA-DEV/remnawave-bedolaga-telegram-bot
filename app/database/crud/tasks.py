"""CRUD операции для системы заданий с наградами."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import (
    Task,
    TaskPartnerChannel,
    TaskRewardType,
    TaskType,
    TaskUserAudience,
    UserTaskProgress,
)


logger = structlog.get_logger(__name__)


# ===========================================================================
# Task CRUD
# ===========================================================================


async def create_task(
    db: AsyncSession,
    *,
    title: dict[str, str],
    description: dict[str, str],
    task_type: TaskType | str,
    reward_type: TaskRewardType | str,
    target_value: int = 1,
    reward_value: int = 0,
    target_meta: dict[str, Any] | None = None,
    reward_meta: dict[str, Any] | None = None,
    icon: str | None = None,
    is_active: bool = True,
    sort_order: int = 0,
    allow_user_choice: bool = False,
    user_audience: TaskUserAudience | str = TaskUserAudience.BOTH,
    promo_group_id: int | None = None,
    parent_task_id: int | None = None,
    level: int = 1,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> Task:
    """Создаёт новый шаблон задания."""
    task = Task(
        title=title,
        description=description,
        icon=icon,
        is_active=is_active,
        sort_order=sort_order,
        task_type=task_type.value if isinstance(task_type, TaskType) else task_type,
        target_value=target_value,
        target_meta=target_meta or {},
        reward_type=reward_type.value if isinstance(reward_type, TaskRewardType) else reward_type,
        reward_value=reward_value,
        reward_meta=reward_meta or {},
        allow_user_choice=allow_user_choice,
        user_audience=user_audience.value if isinstance(user_audience, TaskUserAudience) else user_audience,
        promo_group_id=promo_group_id,
        parent_task_id=parent_task_id,
        level=level,
        starts_at=starts_at,
        ends_at=ends_at,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    logger.info('Создано задание', task_id=task.id, type=task.task_type, level=task.level)
    return task


async def get_task_by_id(db: AsyncSession, task_id: int) -> Task | None:
    result = await db.execute(
        select(Task).options(selectinload(Task.promo_group)).where(Task.id == task_id)
    )
    return result.scalar_one_or_none()


async def list_tasks(
    db: AsyncSession,
    *,
    include_inactive: bool = False,
    parent_task_id: int | None = None,
) -> list[Task]:
    """Список заданий (для админа). По умолчанию исключает неактивные."""
    stmt = (
        select(Task)
        .options(selectinload(Task.promo_group))
        .order_by(Task.sort_order, Task.id)
    )
    if not include_inactive:
        stmt = stmt.where(Task.is_active == True)
    if parent_task_id is not None:
        stmt = stmt.where(Task.parent_task_id == parent_task_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def update_task(db: AsyncSession, task: Task, **fields: Any) -> Task:
    """Обновляет поля задания. Enum-поля принимаются как enum или строка."""
    for key, value in fields.items():
        if key == 'task_type' and isinstance(value, TaskType):
            value = value.value
        if key == 'reward_type' and isinstance(value, TaskRewardType):
            value = value.value
        if key == 'user_audience' and isinstance(value, TaskUserAudience):
            value = value.value
        setattr(task, key, value)
    task.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(task)
    logger.info('Обновлено задание', task_id=task.id)
    return task


async def delete_task(db: AsyncSession, task: Task) -> None:
    await db.delete(task)
    await db.commit()
    logger.info('Удалено задание', task_id=task.id)


async def list_active_tasks_for_user(
    db: AsyncSession,
    *,
    user_audience: TaskUserAudience | str,
    promo_group_id: int | None,
    now: datetime | None = None,
) -> list[Task]:
    """Возвращает активные задания, доступные конкретному пользователю.

    Учитывает:
    - is_active = True
    - starts_at <= now <= ends_at (если заданы)
    - user_audience: задание для 'both' или совпадающего типа аудитории
    - promo_group_id: задание без промогруппы либо совпадающей с user
    """
    audience_value = (
        user_audience.value if isinstance(user_audience, TaskUserAudience) else user_audience
    )
    now = now or datetime.now(UTC)

    stmt = (
        select(Task)
        .where(Task.is_active == True)
        .where((Task.starts_at == None) | (Task.starts_at <= now))
        .where((Task.ends_at == None) | (Task.ends_at >= now))
        .where(Task.user_audience.in_(['both', audience_value]))
        .order_by(Task.level, Task.sort_order, Task.id)
    )
    if promo_group_id is not None:
        # Задание без promo_group_id — для всех; либо ровно та же группа
        stmt = stmt.where((Task.promo_group_id == None) | (Task.promo_group_id == promo_group_id))
    else:
        stmt = stmt.where(Task.promo_group_id == None)

    result = await db.execute(stmt)
    return list(result.scalars().all())


# ===========================================================================
# UserTaskProgress CRUD
# ===========================================================================


async def get_progress(db: AsyncSession, *, user_id: int, task_id: int) -> UserTaskProgress | None:
    result = await db.execute(
        select(UserTaskProgress)
        .options(selectinload(UserTaskProgress.task))
        .where(UserTaskProgress.user_id == user_id, UserTaskProgress.task_id == task_id)
    )
    return result.scalar_one_or_none()


async def get_progress_for_update(
    db: AsyncSession, *, user_id: int, task_id: int
) -> UserTaskProgress | None:
    """FOR UPDATE lock — для атомарного claim."""
    result = await db.execute(
        select(UserTaskProgress)
        .where(UserTaskProgress.user_id == user_id, UserTaskProgress.task_id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_progress_by_id_for_update(db: AsyncSession, progress_id: int) -> UserTaskProgress | None:
    result = await db.execute(
        select(UserTaskProgress)
        .where(UserTaskProgress.id == progress_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_or_create_progress(
    db: AsyncSession,
    *,
    user_id: int,
    task_id: int,
    period_started_at: datetime | None = None,
    baseline_value: int = 0,
) -> tuple[UserTaskProgress, bool]:
    """Получает или создаёт запись прогресса. Возвращает (progress, created).

    На PostgreSQL использует ``INSERT ... ON CONFLICT DO NOTHING`` (атомарный upsert),
    защищая от race на uq_user_task при параллельных record_event.

    На SQLite (dev/test mode) использует savepoint + try/IntegrityError — atomic upsert
    тоже доступен в SQLite dialect, но проще и надёжнее savepoint pattern.
    """
    from sqlalchemy.exc import IntegrityError

    from app.database.database import IS_SQLITE

    if IS_SQLITE:
        existing = await get_progress(db, user_id=user_id, task_id=task_id)
        if existing is not None:
            return existing, False

        # ВАЖНО: ``db.add(progress)`` ДОЛЖЕН быть внутри ``begin_nested()``, иначе
        # SQLAlchemy в ``_take_snapshot`` сделает flush до открытия savepoint, и
        # IntegrityError повредит outer transaction вместо savepoint. См. эталонный
        # паттерн в ``app/database/crud/promocode.py``.
        try:
            async with db.begin_nested():
                progress = UserTaskProgress(
                    user_id=user_id,
                    task_id=task_id,
                    current_value=0,
                    baseline_value=baseline_value,
                    period_started_at=period_started_at,
                )
                db.add(progress)
                await db.flush()
            return progress, True
        except IntegrityError:
            # Параллельный insert проскочил впереди — fetch'нем существующую запись.
            # Savepoint уже откатился, _new очищен через _restore_snapshot.
            existing = await get_progress(db, user_id=user_id, task_id=task_id)
            if existing is None:
                raise RuntimeError('UserTaskProgress race: row disappeared after IntegrityError')
            return existing, False

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    stmt = (
        pg_insert(UserTaskProgress)
        .values(
            user_id=user_id,
            task_id=task_id,
            current_value=0,
            baseline_value=baseline_value,
            period_started_at=period_started_at,
        )
        .on_conflict_do_nothing(index_elements=['user_id', 'task_id'])
        .returning(UserTaskProgress.id)
    )
    result = await db.execute(stmt)
    row = result.first()
    created = row is not None
    if created:
        await db.flush()

    progress = await get_progress(db, user_id=user_id, task_id=task_id)
    if progress is None:
        # Не должно случаться: либо мы только что вставили, либо запись уже была.
        raise RuntimeError('UserTaskProgress disappeared after upsert')
    return progress, created


async def list_user_progress(
    db: AsyncSession, *, user_id: int, task_ids: list[int] | None = None
) -> list[UserTaskProgress]:
    stmt = (
        select(UserTaskProgress)
        .options(selectinload(UserTaskProgress.task))
        .where(UserTaskProgress.user_id == user_id)
    )
    if task_ids is not None:
        stmt = stmt.where(UserTaskProgress.task_id.in_(task_ids))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def mark_progress_completed(
    db: AsyncSession, progress: UserTaskProgress
) -> UserTaskProgress:
    if progress.completed_at is None:
        progress.completed_at = datetime.now(UTC)
        progress.updated_at = datetime.now(UTC)
        await db.flush()
    return progress


async def mark_progress_claimed(
    db: AsyncSession,
    progress: UserTaskProgress,
    *,
    reward_granted_meta: dict[str, Any] | None = None,
) -> UserTaskProgress:
    if progress.claimed_at is None:
        progress.claimed_at = datetime.now(UTC)
    if reward_granted_meta is not None:
        progress.reward_granted_meta = reward_granted_meta
    progress.updated_at = datetime.now(UTC)
    await db.flush()
    return progress


# ===========================================================================
# TaskPartnerChannel CRUD
# ===========================================================================


async def list_partner_channels(db: AsyncSession, *, include_inactive: bool = False) -> list[TaskPartnerChannel]:
    stmt = select(TaskPartnerChannel).order_by(TaskPartnerChannel.sort_order, TaskPartnerChannel.id)
    if not include_inactive:
        stmt = stmt.where(TaskPartnerChannel.is_active == True)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_partner_channel_by_id(db: AsyncSession, channel_pk: int) -> TaskPartnerChannel | None:
    result = await db.execute(
        select(TaskPartnerChannel).where(TaskPartnerChannel.id == channel_pk)
    )
    return result.scalar_one_or_none()


async def get_partner_channel_by_channel_id(
    db: AsyncSession, channel_id: str
) -> TaskPartnerChannel | None:
    result = await db.execute(
        select(TaskPartnerChannel).where(TaskPartnerChannel.channel_id == channel_id)
    )
    return result.scalar_one_or_none()


async def create_partner_channel(
    db: AsyncSession,
    *,
    channel_id: str,
    title: str,
    channel_link: str | None = None,
    description: str | None = None,
    is_active: bool = True,
    sort_order: int = 0,
) -> TaskPartnerChannel:
    channel = TaskPartnerChannel(
        channel_id=channel_id,
        title=title,
        channel_link=channel_link,
        description=description,
        is_active=is_active,
        sort_order=sort_order,
    )
    db.add(channel)
    await db.commit()
    await db.refresh(channel)
    logger.info('Создан партнёрский канал', channel_id=channel_id)
    return channel


async def update_partner_channel(
    db: AsyncSession, channel: TaskPartnerChannel, **fields: Any
) -> TaskPartnerChannel:
    for key, value in fields.items():
        setattr(channel, key, value)
    channel.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(channel)
    return channel


async def delete_partner_channel(db: AsyncSession, channel: TaskPartnerChannel) -> None:
    await db.delete(channel)
    await db.commit()
