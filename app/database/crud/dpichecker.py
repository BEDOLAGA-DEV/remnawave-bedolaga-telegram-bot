"""Строки действий DPI//CHECKER из кабинета (см. модель :class:`DpiCheckerAction`)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import DpiCheckerAction, User


KIND_CHECK = 'check'
KIND_PROBE = 'probe'
KIND_NOISY = 'noisy'
KIND_MONITOR = 'monitor'
LABEL_MAX = 255
DELIVERY_MEMORY = 50


async def create_action(
    db: AsyncSession,
    *,
    kind: str,
    admin_user_id: int | None,
    check_type: str | None,
    location: str | None,
    pop_count: int,
    resource_count: int,
    source: str,
    source_ref: str | None,
    label: str,
    targets: list[dict[str, Any]],
    request: dict[str, Any],
    created_at: datetime | None = None,
) -> DpiCheckerAction:
    """Строка до обращения к сервису: ключ идемпотентности рождается здесь и живёт с ней.

    ``created_at`` задаётся у запуска, взятого с сайта: в истории он стоит на своём месте, а не сверху.
    """
    action = DpiCheckerAction(
        kind=kind,
        admin_user_id=admin_user_id,
        check_type=check_type,
        location=location,
        pop_count=pop_count,
        resource_count=resource_count,
        source=source,
        source_ref=source_ref,
        label=label[:LABEL_MAX],
        targets=list(targets),
        request=dict(request),
        idempotency_key=uuid4().hex,
        status='submitting',
        delivery_ids=[],
        **({'created_at': created_at} if created_at is not None else {}),
    )
    db.add(action)
    await db.flush()
    return action


async def get_action(db: AsyncSession, action_id: int) -> DpiCheckerAction | None:
    return await db.get(DpiCheckerAction, action_id)


async def get_by_remote(db: AsyncSession, kind: str, remote_id: int) -> DpiCheckerAction | None:
    result = await db.execute(
        select(DpiCheckerAction).where(DpiCheckerAction.kind == kind, DpiCheckerAction.remote_id == remote_id)
    )
    return result.scalar_one_or_none()


async def list_actions(
    db: AsyncSession,
    *,
    kind: str | None = None,
    check_type: str | None = None,
    admin_user_id: int | None = None,
    limit: int = 25,
    offset: int = 0,
) -> tuple[list[DpiCheckerAction], int]:
    query = select(DpiCheckerAction)
    if kind:
        query = query.where(DpiCheckerAction.kind == kind)
    if check_type:
        query = query.where(DpiCheckerAction.check_type == check_type)
    if admin_user_id is not None:
        query = query.where(DpiCheckerAction.admin_user_id == admin_user_id)
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = await db.execute(
        query.order_by(DpiCheckerAction.created_at.desc(), DpiCheckerAction.id.desc()).limit(limit).offset(offset)
    )
    return list(rows.scalars()), int(total)


async def list_monitors(db: AsyncSession) -> list[DpiCheckerAction]:
    """Мониторы из кабинета, которые ещё живы у сервиса (для обходчика)."""
    rows = await db.execute(
        select(DpiCheckerAction)
        .where(
            DpiCheckerAction.kind == KIND_MONITOR,
            DpiCheckerAction.remote_id.is_not(None),
            DpiCheckerAction.status != 'deleted',
        )
        .order_by(DpiCheckerAction.id)
    )
    return list(rows.scalars())


async def by_remote(db: AsyncSession, kind: str, remote_ids: list[int]) -> dict[int, DpiCheckerAction]:
    """Свои строки по номерам у сервиса — в любом статусе (имя есть и у отключённого монитора)."""
    if not remote_ids:
        return {}
    rows = await db.execute(
        select(DpiCheckerAction).where(
            DpiCheckerAction.kind == kind, DpiCheckerAction.remote_id.in_(sorted(set(remote_ids)))
        )
    )
    return {int(action.remote_id): action for action in rows.scalars()}


async def claim_delivery(db: AsyncSession, action: DpiCheckerAction, delivery_id: int) -> bool:
    """True, если доставку вебхука видим впервые (и запоминаем её)."""
    seen = [int(item) for item in action.delivery_ids or []]
    if delivery_id in seen:
        return False
    action.delivery_ids = [*seen, delivery_id][-DELIVERY_MEMORY:]
    await db.flush()
    return True


FILTERS_BY_TYPE = ('vpn', 'ip', 'mtproto')
FILTERS_BY_KIND = (KIND_NOISY, KIND_PROBE)


async def count_by_filter(db: AsyncSession, *, admin_user_id: int | None = None) -> dict[str, int]:
    """Сколько запусков у каждого фильтра истории: все, проверки по типу, Соседи, Зонд."""
    query = select(DpiCheckerAction.kind, DpiCheckerAction.check_type, func.count()).group_by(
        DpiCheckerAction.kind, DpiCheckerAction.check_type
    )
    if admin_user_id is not None:
        query = query.where(DpiCheckerAction.admin_user_id == admin_user_id)
    counts = dict.fromkeys(('all', *FILTERS_BY_TYPE, *FILTERS_BY_KIND), 0)
    for kind, check_type, count in (await db.execute(query)).all():
        counts['all'] += count
        if kind == KIND_CHECK and check_type in FILTERS_BY_TYPE:
            counts[check_type] += count
        elif kind in FILTERS_BY_KIND:
            counts[kind] += count
    return counts


async def admin_names(db: AsyncSession, user_ids: list[int]) -> dict[int, str]:
    """Имена админов для строк истории — как их видно в кабинете, а не «админ #id»."""
    wanted = sorted(set(user_ids))
    if not wanted:
        return {}
    rows = await db.execute(select(User).where(User.id.in_(wanted)))
    return {user.id: user.full_name for user in rows.scalars()}
