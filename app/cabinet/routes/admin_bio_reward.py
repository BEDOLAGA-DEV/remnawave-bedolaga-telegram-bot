"""Admin REST API for the bio-reward feature.

Endpoints:
    GET    /admin/bio-reward/config                       -> current singleton config
    PUT    /admin/bio-reward/config                       -> update config fields
    GET    /admin/bio-reward/stats                        -> aggregate counts
    GET    /admin/bio-reward/participants                 -> paginated participant list
    GET    /admin/bio-reward/participants/{id}            -> participant detail + events
    POST   /admin/bio-reward/participants/{id}/revoke     -> force revoke now
    POST   /admin/bio-reward/participants/{id}/restore    -> clear cooldown, set PENDING
    POST   /admin/bio-reward/participants/{id}/bypass     -> toggle bypass_check flag
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.crud import bio_reward as bio_crud
from app.database.models import BioRewardParticipant, BioRewardStatus, User
from app.services.bio_reward_service import bio_reward_service

from ..dependencies import get_cabinet_db, require_permission


router = APIRouter(prefix='/admin/bio-reward', tags=['Admin Bio Reward'])


# ============== Schemas ==============


class BioRewardConfigResponse(BaseModel):
    enabled: bool
    discount_percent: int
    grace_period_hours: int
    cooldown_hours: int
    check_interval_minutes: int
    free_sub_window_days: int
    free_sub_traffic_gb_per_day: int
    free_sub_device_limit: int
    free_sub_squad_uuid: str | None = None
    accepted_bio_strings: list[str] = Field(default_factory=list)
    match_personal_referral_link: bool
    notify_on_opt_in: bool
    notify_on_activate: bool
    notify_on_grace: bool
    notify_on_revoke: bool
    instruction_text: str | None = None
    updated_at: datetime | None = None


class BioRewardConfigUpdate(BaseModel):
    enabled: bool | None = None
    discount_percent: int | None = Field(default=None, ge=0, le=100)
    grace_period_hours: int | None = Field(default=None, ge=0, le=720)
    cooldown_hours: int | None = Field(default=None, ge=0, le=8760)
    check_interval_minutes: int | None = Field(default=None, ge=1, le=1440)
    free_sub_window_days: int | None = Field(default=None, ge=1, le=30)
    free_sub_traffic_gb_per_day: int | None = Field(default=None, ge=0, le=10000)
    free_sub_device_limit: int | None = Field(default=None, ge=1, le=100)
    free_sub_squad_uuid: str | None = None
    accepted_bio_strings: list[str] | None = None
    match_personal_referral_link: bool | None = None
    notify_on_opt_in: bool | None = None
    notify_on_activate: bool | None = None
    notify_on_grace: bool | None = None
    notify_on_revoke: bool | None = None
    instruction_text: str | None = None


class BioRewardParticipantResponse(BaseModel):
    id: int
    user_id: int
    telegram_id: int | None = None
    username: str | None = None
    full_name: str | None = None
    status: str
    opted_in_at: datetime | None = None
    last_check_at: datetime | None = None
    last_bio_seen_at: datetime | None = None
    grace_started_at: datetime | None = None
    revoked_at: datetime | None = None
    cooldown_until: datetime | None = None
    free_subscription_id: int | None = None
    bio_snapshot: str | None = None
    bypass_check: bool


class BioRewardParticipantListResponse(BaseModel):
    items: list[BioRewardParticipantResponse]
    total: int
    limit: int
    offset: int


class BioRewardEventResponse(BaseModel):
    id: int
    event_type: str
    payload: dict[str, Any] | None = None
    created_at: datetime | None = None


class BioRewardParticipantDetailResponse(BioRewardParticipantResponse):
    recent_events: list[BioRewardEventResponse] = Field(default_factory=list)


class BioRewardStatsResponse(BaseModel):
    total_participants: int
    active: int
    grace: int
    cooldown: int
    revoked: int
    pending: int
    revocations_last_24h: int


class BypassRequest(BaseModel):
    enabled: bool


# ============== Helpers ==============


def _serialize_participant(participant: BioRewardParticipant) -> BioRewardParticipantResponse:
    user = participant.user
    return BioRewardParticipantResponse(
        id=participant.id,
        user_id=participant.user_id,
        telegram_id=getattr(user, 'telegram_id', None) if user else None,
        username=getattr(user, 'username', None) if user else None,
        full_name=getattr(user, 'full_name', None) if user else None,
        status=participant.status,
        opted_in_at=participant.opted_in_at,
        last_check_at=participant.last_check_at,
        last_bio_seen_at=participant.last_bio_seen_at,
        grace_started_at=participant.grace_started_at,
        revoked_at=participant.revoked_at,
        cooldown_until=participant.cooldown_until,
        free_subscription_id=participant.free_subscription_id,
        bio_snapshot=participant.bio_snapshot,
        bypass_check=bool(participant.bypass_check),
    )


def _config_to_response(cfg) -> BioRewardConfigResponse:
    return BioRewardConfigResponse(
        enabled=bool(cfg.enabled),
        discount_percent=int(cfg.discount_percent),
        grace_period_hours=int(cfg.grace_period_hours),
        cooldown_hours=int(cfg.cooldown_hours),
        check_interval_minutes=int(cfg.check_interval_minutes),
        free_sub_window_days=int(cfg.free_sub_window_days),
        free_sub_traffic_gb_per_day=int(cfg.free_sub_traffic_gb_per_day),
        free_sub_device_limit=int(cfg.free_sub_device_limit),
        free_sub_squad_uuid=cfg.free_sub_squad_uuid,
        accepted_bio_strings=list(cfg.accepted_bio_strings or []),
        match_personal_referral_link=bool(cfg.match_personal_referral_link),
        notify_on_opt_in=bool(cfg.notify_on_opt_in),
        notify_on_activate=bool(cfg.notify_on_activate),
        notify_on_grace=bool(cfg.notify_on_grace),
        notify_on_revoke=bool(cfg.notify_on_revoke),
        instruction_text=cfg.instruction_text,
        updated_at=cfg.updated_at,
    )


# ============== Config endpoints ==============


@router.get('/config', response_model=BioRewardConfigResponse)
async def get_config_endpoint(
    admin: User = Depends(require_permission('settings:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BioRewardConfigResponse:
    cfg = await bio_crud.get_config(db)
    return _config_to_response(cfg)


@router.put('/config', response_model=BioRewardConfigResponse)
async def update_config_endpoint(
    payload: BioRewardConfigUpdate,
    admin: User = Depends(require_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BioRewardConfigResponse:
    fields = payload.model_dump(exclude_unset=True)
    if 'accepted_bio_strings' in fields and fields['accepted_bio_strings'] is not None:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in fields['accepted_bio_strings']:
            s = (raw or '').strip()
            if not s or s.lower() in seen:
                continue
            seen.add(s.lower())
            cleaned.append(s)
        fields['accepted_bio_strings'] = cleaned

    cfg = await bio_crud.update_config(db, **fields)
    return _config_to_response(cfg)


# ============== Stats ==============


@router.get('/stats', response_model=BioRewardStatsResponse)
async def stats_endpoint(
    admin: User = Depends(require_permission('settings:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BioRewardStatsResponse:
    counts_stmt = select(BioRewardParticipant.status, func.count(BioRewardParticipant.id)).group_by(
        BioRewardParticipant.status
    )
    counts_result = await db.execute(counts_stmt)
    counts: dict[str, int] = {row[0]: int(row[1]) for row in counts_result.all()}

    cutoff = datetime.now(UTC) - timedelta(hours=24)
    recent_revokes_result = await db.execute(
        select(func.count(BioRewardParticipant.id)).where(
            BioRewardParticipant.revoked_at.isnot(None),
            BioRewardParticipant.revoked_at >= cutoff,
        )
    )
    recent_revokes = int(recent_revokes_result.scalar() or 0)

    return BioRewardStatsResponse(
        total_participants=sum(counts.values()),
        active=counts.get(BioRewardStatus.ACTIVE.value, 0),
        grace=counts.get(BioRewardStatus.GRACE.value, 0),
        cooldown=counts.get(BioRewardStatus.COOLDOWN.value, 0),
        revoked=counts.get(BioRewardStatus.REVOKED.value, 0),
        pending=counts.get(BioRewardStatus.PENDING.value, 0),
        revocations_last_24h=recent_revokes,
    )


# ============== Participant endpoints ==============


@router.get('/participants', response_model=BioRewardParticipantListResponse)
async def list_participants_endpoint(
    admin: User = Depends(require_permission('settings:read')),
    db: AsyncSession = Depends(get_cabinet_db),
    status_filter: str | None = Query(default=None, alias='status'),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> BioRewardParticipantListResponse:
    base_filters = []
    if status_filter:
        base_filters.append(BioRewardParticipant.status == status_filter)

    count_stmt = select(func.count(BioRewardParticipant.id))
    if base_filters:
        count_stmt = count_stmt.where(*base_filters)
    total = int((await db.execute(count_stmt)).scalar() or 0)

    rows_stmt = (
        select(BioRewardParticipant)
        .options(selectinload(BioRewardParticipant.user))
        .order_by(BioRewardParticipant.opted_in_at.desc())
        .offset(offset)
        .limit(limit)
    )
    if base_filters:
        rows_stmt = rows_stmt.where(*base_filters)

    rows_result = await db.execute(rows_stmt)
    rows = list(rows_result.scalars().all())
    return BioRewardParticipantListResponse(
        items=[_serialize_participant(p) for p in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


async def _get_participant_or_404(
    db: AsyncSession, participant_id: int
) -> BioRewardParticipant:
    result = await db.execute(
        select(BioRewardParticipant)
        .where(BioRewardParticipant.id == participant_id)
        .options(selectinload(BioRewardParticipant.user))
    )
    participant = result.scalar_one_or_none()
    if participant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Participant not found')
    return participant


@router.get(
    '/participants/{participant_id}', response_model=BioRewardParticipantDetailResponse
)
async def get_participant_endpoint(
    participant_id: int,
    admin: User = Depends(require_permission('settings:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BioRewardParticipantDetailResponse:
    participant = await _get_participant_or_404(db, participant_id)
    events = await bio_crud.list_events(db, participant_id, limit=50)
    base = _serialize_participant(participant)
    return BioRewardParticipantDetailResponse(
        **base.model_dump(),
        recent_events=[
            BioRewardEventResponse(
                id=e.id, event_type=e.event_type, payload=e.payload, created_at=e.created_at
            )
            for e in events
        ],
    )


@router.post(
    '/participants/{participant_id}/revoke', response_model=BioRewardParticipantResponse
)
async def force_revoke_endpoint(
    participant_id: int,
    admin: User = Depends(require_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BioRewardParticipantResponse:
    participant = await _get_participant_or_404(db, participant_id)
    if participant.user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Linked user missing')

    cfg = await bio_crud.get_config(db)
    await bio_reward_service._revoke(db, participant, participant.user, cfg)
    await bio_crud.log_event(db, participant.id, 'admin_force_revoke', {'admin_id': admin.id})
    await db.refresh(participant)
    return _serialize_participant(participant)


@router.post(
    '/participants/{participant_id}/restore', response_model=BioRewardParticipantResponse
)
async def restore_endpoint(
    participant_id: int,
    admin: User = Depends(require_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BioRewardParticipantResponse:
    participant = await _get_participant_or_404(db, participant_id)
    await bio_crud.set_status(
        db,
        participant,
        BioRewardStatus.PENDING,
        cooldown_until=None,
        grace_started_at=None,
        revoked_at=None,
    )
    await bio_crud.log_event(db, participant.id, 'admin_restore', {'admin_id': admin.id})
    await db.refresh(participant)
    return _serialize_participant(participant)


@router.post(
    '/participants/{participant_id}/bypass', response_model=BioRewardParticipantResponse
)
async def toggle_bypass_endpoint(
    participant_id: int,
    payload: BypassRequest,
    admin: User = Depends(require_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BioRewardParticipantResponse:
    participant = await _get_participant_or_404(db, participant_id)
    participant.bypass_check = bool(payload.enabled)
    await db.commit()
    await db.refresh(participant)
    await bio_crud.log_event(
        db,
        participant.id,
        'admin_bypass_toggle',
        {'admin_id': admin.id, 'enabled': bool(payload.enabled)},
    )
    return _serialize_participant(participant)
