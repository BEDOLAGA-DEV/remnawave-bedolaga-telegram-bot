"""CRUD for the bio-reward feature (config singleton, participants, events)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import (
    BioRewardConfig,
    BioRewardEvent,
    BioRewardParticipant,
    BioRewardStatus,
    Subscription,
)


# ---------- Config (singleton row) ----------


async def get_config(db: AsyncSession) -> BioRewardConfig:
    """Return the singleton config row, creating defaults if missing."""
    result = await db.execute(select(BioRewardConfig).order_by(BioRewardConfig.id.asc()).limit(1))
    cfg = result.scalar_one_or_none()
    if cfg is None:
        cfg = BioRewardConfig()
        db.add(cfg)
        await db.commit()
        await db.refresh(cfg)
    return cfg


async def update_config(db: AsyncSession, **fields: Any) -> BioRewardConfig:
    cfg = await get_config(db)
    for key, value in fields.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    await db.commit()
    await db.refresh(cfg)
    return cfg


# ---------- Participant ----------


async def get_participant_by_user_id(
    db: AsyncSession, user_id: int, *, with_subscription: bool = False
) -> BioRewardParticipant | None:
    stmt = select(BioRewardParticipant).where(BioRewardParticipant.user_id == user_id)
    if with_subscription:
        stmt = stmt.options(selectinload(BioRewardParticipant.free_subscription))
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_or_create_participant(
    db: AsyncSession, user_id: int
) -> tuple[BioRewardParticipant, bool]:
    """Return (participant, created_flag)."""
    participant = await get_participant_by_user_id(db, user_id)
    if participant is not None:
        return participant, False
    participant = BioRewardParticipant(
        user_id=user_id,
        status=BioRewardStatus.PENDING.value,
        opted_in_at=datetime.now(UTC),
    )
    db.add(participant)
    await db.commit()
    await db.refresh(participant)
    return participant, True


async def list_participants_for_check(
    db: AsyncSession, *, batch_size: int = 1000
) -> list[BioRewardParticipant]:
    """Return participants whose state requires periodic bio re-check."""
    states = (
        BioRewardStatus.PENDING.value,
        BioRewardStatus.ACTIVE.value,
        BioRewardStatus.GRACE.value,
    )
    stmt = (
        select(BioRewardParticipant)
        .where(BioRewardParticipant.status.in_(states))
        .options(
            selectinload(BioRewardParticipant.user),
            selectinload(BioRewardParticipant.free_subscription),
        )
        .limit(batch_size)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def list_participants_in_cooldown_due(
    db: AsyncSession, *, now: datetime | None = None
) -> list[BioRewardParticipant]:
    now = now or datetime.now(UTC)
    stmt = select(BioRewardParticipant).where(
        BioRewardParticipant.status == BioRewardStatus.COOLDOWN.value,
        BioRewardParticipant.cooldown_until <= now,
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def set_status(
    db: AsyncSession,
    participant: BioRewardParticipant,
    status: BioRewardStatus | str,
    **extra: Any,
) -> BioRewardParticipant:
    participant.status = status.value if isinstance(status, BioRewardStatus) else status
    for key, value in extra.items():
        if hasattr(participant, key):
            setattr(participant, key, value)
    await db.commit()
    await db.refresh(participant)
    return participant


async def attach_free_subscription(
    db: AsyncSession, participant: BioRewardParticipant, subscription: Subscription
) -> None:
    participant.free_subscription_id = subscription.id
    await db.commit()


async def count_active_participants(db: AsyncSession) -> int:
    stmt = select(BioRewardParticipant).where(
        BioRewardParticipant.status == BioRewardStatus.ACTIVE.value
    )
    result = await db.execute(stmt)
    return len(result.scalars().all())


# ---------- Event audit log ----------


async def log_event(
    db: AsyncSession,
    participant_id: int,
    event_type: str,
    payload: dict | None = None,
    *,
    commit: bool = True,
) -> BioRewardEvent:
    event = BioRewardEvent(
        participant_id=participant_id,
        event_type=event_type,
        payload=payload or {},
    )
    db.add(event)
    if commit:
        await db.commit()
        await db.refresh(event)
    return event


async def list_events(
    db: AsyncSession, participant_id: int, *, limit: int = 100
) -> list[BioRewardEvent]:
    stmt = (
        select(BioRewardEvent)
        .where(BioRewardEvent.participant_id == participant_id)
        .order_by(BioRewardEvent.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())
