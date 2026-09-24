"""Строки действий DPI//CHECKER из кабинета (см. модель :class:`DpiCheckerAction`)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import DpiCheckerAction


KIND_CHECK = 'check'
KIND_PROBE = 'probe'
KIND_NOISY = 'noisy'
KIND_MONITOR = 'monitor'
LABEL_MAX = 255
DELIVERY_MEMORY = 50
USD_CENTS = Decimal('0.0001')


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
) -> DpiCheckerAction:
    """Строка до обращения к сервису: ключ идемпотентности рождается здесь и живёт с ней."""
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


async def claim_delivery(db: AsyncSession, action: DpiCheckerAction, delivery_id: int) -> bool:
    """True, если доставку вебхука видим впервые (и запоминаем её)."""
    seen = [int(item) for item in action.delivery_ids or []]
    if delivery_id in seen:
        return False
    action.delivery_ids = [*seen, delivery_id][-DELIVERY_MEMORY:]
    await db.flush()
    return True


async def spend_by_admin(db: AsyncSession) -> list[tuple[int | None, Decimal]]:
    """Потрачено по админам: списания минус возвраты; запуски без цены не считаются."""
    spent = func.coalesce(func.sum(DpiCheckerAction.cost_usd), 0) - func.coalesce(
        func.sum(DpiCheckerAction.refunded_usd), 0
    )
    rows = await db.execute(
        select(DpiCheckerAction.admin_user_id, spent)
        .where(DpiCheckerAction.cost_usd.is_not(None))
        .group_by(DpiCheckerAction.admin_user_id)
        .order_by(DpiCheckerAction.admin_user_id)
    )
    return [(admin_id, Decimal(value).quantize(USD_CENTS)) for admin_id, value in rows.all()]
