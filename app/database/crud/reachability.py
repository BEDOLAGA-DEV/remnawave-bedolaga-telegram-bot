"""CRUD раздела BSCHEKER: задачи, пачки проверок, леги, предпочтения по целям."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Text, and_, delete, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import ReachabilityBatch, ReachabilityJob, ReachabilityLeg, ReachabilityTargetPref


JOB_STATUSES = ('pending', 'running', 'done', 'failed', 'cancelled')
ACTIVE_STATUSES = ('pending', 'running')
TERMINAL_STATUSES = ('done', 'failed', 'cancelled')


async def create_job(db: AsyncSession, **fields: Any) -> ReachabilityJob:
    job = ReachabilityJob(**fields)
    db.add(job)
    await db.flush()
    # Свежий объект не знает своих легов: обращение к ``job.legs`` в обработчике
    # запустило бы ленивый SELECT вне greenlet (MissingGreenlet). Задача отдаётся
    # готовой к ответу, как из get_job/list_jobs с selectinload.
    await db.refresh(job, attribute_names=['legs'])
    return job


async def get_job(db: AsyncSession, job_id: int) -> ReachabilityJob | None:
    result = await db.execute(
        select(ReachabilityJob).options(selectinload(ReachabilityJob.legs)).where(ReachabilityJob.id == job_id)
    )
    return result.scalar_one_or_none()


async def update_job(db: AsyncSession, job: ReachabilityJob, **fields: Any) -> ReachabilityJob:
    for name, value in fields.items():
        setattr(job, name, value)
    job.updated_at = datetime.now(UTC)
    await db.flush()
    return job


async def list_jobs(
    db: AsyncSession,
    *,
    kind: str | None = None,
    status: str | None = None,
    target_key: str | None = None,
    user_id: int | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[ReachabilityJob], int]:
    conditions = []
    if kind:
        conditions.append(ReachabilityJob.kind == kind)
    if status:
        conditions.append(ReachabilityJob.status == status)
    if user_id is not None:
        conditions.append(ReachabilityJob.started_by_user_id == user_id)
    if target_key:
        # targets — JSON-список; ищем подстроку "target_key": "…" в его текстовом виде,
        # это работает одинаково в SQLite и PostgreSQL без JSON-операторов.
        conditions.append(func.cast(ReachabilityJob.targets, Text).like(f'%"target_key": "{target_key}"%'))
    where = and_(*conditions) if conditions else true()

    total = (await db.execute(select(func.count()).select_from(ReachabilityJob).where(where))).scalar_one()
    rows = await db.execute(
        select(ReachabilityJob)
        .options(selectinload(ReachabilityJob.legs))
        .where(where)
        .order_by(ReachabilityJob.created_at.desc(), ReachabilityJob.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(rows.scalars().all()), int(total)


async def get_active_job(db: AsyncSession, kind: str) -> ReachabilityJob | None:
    result = await db.execute(
        select(ReachabilityJob)
        .where(ReachabilityJob.kind == kind, ReachabilityJob.status.in_(ACTIVE_STATUSES))
        .order_by(ReachabilityJob.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_unfinished_jobs(db: AsyncSession) -> list[ReachabilityJob]:
    result = await db.execute(
        select(ReachabilityJob).where(ReachabilityJob.status.in_(ACTIVE_STATUSES)).order_by(ReachabilityJob.id)
    )
    return list(result.scalars().all())


async def replace_legs(db: AsyncSession, job_id: int, legs: list[dict[str, Any]]) -> list[ReachabilityLeg]:
    await db.execute(delete(ReachabilityLeg).where(ReachabilityLeg.job_id == job_id))
    rows = [ReachabilityLeg(job_id=job_id, **leg) for leg in legs]
    db.add_all(rows)
    await db.flush()
    # Леги пишутся мимо ORM-коллекции; у задачи, уже загруженной в этом сеансе,
    # ``legs`` остался бы прежним. Обновляем явно, чтобы объект отвечал базе.
    job = await db.get(ReachabilityJob, job_id)
    if job is not None:
        await db.refresh(job, attribute_names=['legs'])
    return rows


async def latest_legs(
    db: AsyncSession, *, target_kind: str | None = None, dpi: str | None = None
) -> list[ReachabilityLeg]:
    """Последний лег на каждую пару (target_key, op_key)."""
    newest = (
        select(
            ReachabilityLeg.target_key.label('target_key'),
            ReachabilityLeg.op_key.label('op_key'),
            func.max(ReachabilityLeg.checked_at).label('checked_at'),
        )
        .group_by(ReachabilityLeg.target_key, ReachabilityLeg.op_key)
        .subquery()
    )
    query = select(ReachabilityLeg).join(
        newest,
        and_(
            ReachabilityLeg.target_key == newest.c.target_key,
            ReachabilityLeg.op_key == newest.c.op_key,
            ReachabilityLeg.checked_at == newest.c.checked_at,
        ),
    )
    if target_kind:
        query = query.where(ReachabilityLeg.target_kind == target_kind)
    if dpi:
        query = query.where(ReachabilityLeg.dpi == dpi)
    result = await db.execute(query.order_by(ReachabilityLeg.target_key, ReachabilityLeg.op_key))
    return list(result.scalars().all())


async def get_pref(db: AsyncSession, target_kind: str, target_ref: str) -> ReachabilityTargetPref | None:
    result = await db.execute(
        select(ReachabilityTargetPref).where(
            ReachabilityTargetPref.target_kind == target_kind, ReachabilityTargetPref.target_ref == target_ref
        )
    )
    return result.scalar_one_or_none()


async def upsert_pref(
    db: AsyncSession,
    *,
    target_kind: str,
    target_ref: str,
    purpose: str | None = None,
    excluded: bool | None = None,
    note: str | None = None,
    user_id: int | None = None,
) -> ReachabilityTargetPref:
    pref = await get_pref(db, target_kind, target_ref)
    if pref is None:
        pref = ReachabilityTargetPref(target_kind=target_kind, target_ref=target_ref)
        db.add(pref)
    if purpose is not None:
        pref.purpose = purpose
    if excluded is not None:
        pref.excluded = excluded
    if note is not None:
        pref.note = note
    pref.updated_by_user_id = user_id
    pref.updated_at = datetime.now(UTC)
    await db.flush()
    return pref


async def list_prefs(db: AsyncSession) -> list[ReachabilityTargetPref]:
    result = await db.execute(select(ReachabilityTargetPref).order_by(ReachabilityTargetPref.id))
    return list(result.scalars().all())


async def last_vless_leg_price_kopeks(db: AsyncSession) -> int | None:
    """Цена одного лега VLESS по последней завершённой задаче (cost / (серверы × симки))."""
    result = await db.execute(
        select(ReachabilityJob)
        .where(
            ReachabilityJob.kind == 'vless',
            ReachabilityJob.status == 'done',
            ReachabilityJob.cost_kopeks.is_not(None),
        )
        .order_by(ReachabilityJob.id.desc())
        .limit(1)
    )
    job = result.scalar_one_or_none()
    if job is None or not job.result:
        return None
    # Сервис задач хранит ответ на запуск под ключом "submit"; допускаем и плоскую форму.
    submit = job.result.get('submit') or job.result
    legs = int(submit.get('n_servers') or 0) * int(submit.get('n_modems') or 0)
    return round(job.cost_kopeks / legs) if legs else None


# ------------------------------------------------------------------ пачки проверок


async def create_batch(db: AsyncSession, **fields: Any) -> ReachabilityBatch:
    batch = ReachabilityBatch(**fields)
    db.add(batch)
    await db.flush()
    await db.refresh(batch, attribute_names=['jobs'])
    return batch


async def get_batch(db: AsyncSession, batch_id: int) -> ReachabilityBatch | None:
    result = await db.execute(
        select(ReachabilityBatch)
        .options(selectinload(ReachabilityBatch.jobs).selectinload(ReachabilityJob.legs))
        .where(ReachabilityBatch.id == batch_id)
    )
    return result.scalar_one_or_none()


async def update_batch(db: AsyncSession, batch: ReachabilityBatch, **fields: Any) -> ReachabilityBatch:
    for name, value in fields.items():
        setattr(batch, name, value)
    batch.updated_at = datetime.now(UTC)
    await db.flush()
    return batch


async def list_batches(db: AsyncSession, *, offset: int = 0, limit: int = 20) -> tuple[list[ReachabilityBatch], int]:
    total = (await db.execute(select(func.count()).select_from(ReachabilityBatch))).scalar_one()
    rows = await db.execute(
        select(ReachabilityBatch)
        .options(selectinload(ReachabilityBatch.jobs).selectinload(ReachabilityJob.legs))
        .order_by(ReachabilityBatch.created_at.desc(), ReachabilityBatch.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(rows.scalars().all()), int(total)


async def list_unfinished_batches(db: AsyncSession) -> list[ReachabilityBatch]:
    result = await db.execute(
        select(ReachabilityBatch).where(ReachabilityBatch.status.in_(ACTIVE_STATUSES)).order_by(ReachabilityBatch.id)
    )
    return list(result.scalars().all())


async def get_active_batch(db: AsyncSession) -> ReachabilityBatch | None:
    result = await db.execute(
        select(ReachabilityBatch)
        .where(ReachabilityBatch.status.in_(ACTIVE_STATUSES))
        .order_by(ReachabilityBatch.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def jobs_for_batch(db: AsyncSession, batch_id: int) -> list[ReachabilityJob]:
    result = await db.execute(
        select(ReachabilityJob)
        .options(selectinload(ReachabilityJob.legs))
        .where(ReachabilityJob.batch_id == batch_id)
        .order_by(ReachabilityJob.id)
    )
    return list(result.scalars().all())
