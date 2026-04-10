"""Admin scheduled promos routes for cabinet."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.scheduled_promo import ScheduledPromoCRUD
from app.database.models import ScheduledPromo, User

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/scheduled-promos', tags=['Cabinet Admin Scheduled Promos'])


# ============== Schemas ==============


class ScheduledPromoResponse(BaseModel):
    id: int
    name: str
    discount_percent: int
    tariff_ids: list[int] = Field(default_factory=list)
    promo_text: str | None = None
    start_at: datetime
    end_at: datetime
    is_active: bool
    created_by: int | None = None
    created_at: datetime | None = None
    status: Literal['active', 'upcoming', 'expired', 'inactive']


class ScheduledPromoListResponse(BaseModel):
    items: list[ScheduledPromoResponse]
    total: int
    limit: int
    offset: int


class ScheduledPromoCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    discount_percent: int = Field(..., ge=0, le=100)
    tariff_ids: list[int] = Field(default_factory=list)
    promo_text: str | None = Field(None, max_length=10000)
    start_at: datetime
    end_at: datetime
    is_active: bool = True

    @field_validator('start_at', 'end_at', mode='after')
    @classmethod
    def ensure_aware_datetime(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            v = v.replace(tzinfo=UTC)
        return v

    @field_validator('tariff_ids', mode='after')
    @classmethod
    def validate_tariff_ids(cls, v: list[int]) -> list[int]:
        return [int(tid) for tid in v if int(tid) > 0]

    @model_validator(mode='after')
    def validate_dates(self) -> 'ScheduledPromoCreateRequest':
        if self.start_at >= self.end_at:
            raise ValueError('start_at must be before end_at')
        return self


class ScheduledPromoUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    discount_percent: int | None = Field(None, ge=0, le=100)
    tariff_ids: list[int] | None = None
    promo_text: str | None = Field(None, max_length=10000)
    start_at: datetime | None = None
    end_at: datetime | None = None
    is_active: bool | None = None

    @field_validator('start_at', 'end_at', mode='after')
    @classmethod
    def ensure_aware_datetime(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            v = v.replace(tzinfo=UTC)
        return v

    @field_validator('tariff_ids', mode='after')
    @classmethod
    def validate_tariff_ids(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return None
        return [int(tid) for tid in v if int(tid) > 0]


# ============== Helpers ==============


def _compute_status(promo: ScheduledPromo) -> Literal['active', 'upcoming', 'expired', 'inactive']:
    if not promo.is_active:
        return 'inactive'
    now = datetime.now(UTC)
    start = promo.start_at
    end = promo.end_at
    # Ensure timezones
    if start and start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end and end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    if start and start > now:
        return 'upcoming'
    if end and end < now:
        return 'expired'
    return 'active'


def _serialize_promo(promo: ScheduledPromo) -> ScheduledPromoResponse:
    tariff_ids = promo.tariff_ids or []
    if not isinstance(tariff_ids, list):
        tariff_ids = []
    return ScheduledPromoResponse(
        id=promo.id,
        name=promo.name,
        discount_percent=int(promo.discount_percent),
        tariff_ids=[int(tid) for tid in tariff_ids],
        promo_text=promo.promo_text,
        start_at=promo.start_at,
        end_at=promo.end_at,
        is_active=bool(promo.is_active),
        created_by=promo.created_by,
        created_at=promo.created_at,
        status=_compute_status(promo),
    )


def _filter_by_status(
    promos: list[ScheduledPromo],
    status_filter: str | None,
) -> list[ScheduledPromo]:
    if not status_filter or status_filter == 'all':
        return promos
    return [p for p in promos if _compute_status(p) == status_filter]


# ============== Endpoints ==============


@router.get('/active', response_model=list[ScheduledPromoResponse])
async def list_active_promos(
    admin: User = Depends(require_permission('scheduled_promos:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> list[ScheduledPromoResponse]:
    """Get currently active scheduled promos."""
    promos = await ScheduledPromoCRUD.get_active_promos(db)
    return [_serialize_promo(p) for p in promos]


@router.get('', response_model=ScheduledPromoListResponse)
async def list_promos(
    admin: User = Depends(require_permission('scheduled_promos:read')),
    db: AsyncSession = Depends(get_cabinet_db),
    status_filter: Literal['all', 'active', 'upcoming', 'expired', 'inactive'] = Query(
        'all', alias='status'
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> ScheduledPromoListResponse:
    """Get list of scheduled promos with filtering and pagination."""
    all_promos = await ScheduledPromoCRUD.get_all_promos(db)
    filtered = _filter_by_status(all_promos, status_filter)
    total = len(filtered)
    paged = filtered[offset : offset + limit]

    return ScheduledPromoListResponse(
        items=[_serialize_promo(p) for p in paged],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get('/{promo_id}', response_model=ScheduledPromoResponse)
async def get_promo(
    promo_id: int,
    admin: User = Depends(require_permission('scheduled_promos:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ScheduledPromoResponse:
    """Get single scheduled promo by ID."""
    result = await db.execute(
        select(ScheduledPromo).where(ScheduledPromo.id == promo_id)
    )
    promo = result.scalar_one_or_none()
    if not promo:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Scheduled promo not found')
    return _serialize_promo(promo)


@router.post('', response_model=ScheduledPromoResponse, status_code=status.HTTP_201_CREATED)
async def create_promo(
    payload: ScheduledPromoCreateRequest,
    admin: User = Depends(require_permission('scheduled_promos:create')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ScheduledPromoResponse:
    """Create a new scheduled promo."""
    promo = await ScheduledPromoCRUD.create_promo(
        db,
        name=payload.name.strip(),
        discount_percent=payload.discount_percent,
        start_at=payload.start_at,
        end_at=payload.end_at,
        tariff_ids=payload.tariff_ids,
        promo_text=payload.promo_text,
        created_by=admin.id,
    )
    # Set is_active if needed (CRUD defaults to True via model default)
    if payload.is_active is False:
        promo.is_active = False
        await db.flush()
        await db.refresh(promo)
    await db.commit()
    logger.info(
        'Admin created scheduled promo via cabinet',
        promo_id=promo.id,
        admin_id=admin.id,
    )
    return _serialize_promo(promo)


@router.patch('/{promo_id}', response_model=ScheduledPromoResponse)
async def update_promo(
    promo_id: int,
    payload: ScheduledPromoUpdateRequest,
    admin: User = Depends(require_permission('scheduled_promos:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ScheduledPromoResponse:
    """Update a scheduled promo."""
    result = await db.execute(
        select(ScheduledPromo).where(ScheduledPromo.id == promo_id)
    )
    promo = result.scalar_one_or_none()
    if not promo:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Scheduled promo not found')

    if payload.name is not None:
        promo.name = payload.name.strip()
    if payload.discount_percent is not None:
        promo.discount_percent = payload.discount_percent
    if payload.tariff_ids is not None:
        promo.tariff_ids = payload.tariff_ids
    if payload.promo_text is not None:
        promo.promo_text = payload.promo_text
    if payload.start_at is not None:
        promo.start_at = payload.start_at
    if payload.end_at is not None:
        promo.end_at = payload.end_at
    if payload.is_active is not None:
        promo.is_active = payload.is_active

    # Validate dates after merging
    start = promo.start_at
    end = promo.end_at
    if start and start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end and end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    if start and end and start >= end:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, 'start_at must be before end_at'
        )

    await db.flush()
    await db.refresh(promo)
    await db.commit()
    logger.info(
        'Admin updated scheduled promo via cabinet',
        promo_id=promo.id,
        admin_id=admin.id,
    )
    return _serialize_promo(promo)


@router.delete('/{promo_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_promo(
    promo_id: int,
    admin: User = Depends(require_permission('scheduled_promos:delete')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> None:
    """Delete a scheduled promo."""
    deleted = await ScheduledPromoCRUD.delete_promo(db, promo_id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Scheduled promo not found')
    await db.commit()
    logger.info(
        'Admin deleted scheduled promo via cabinet',
        promo_id=promo_id,
        admin_id=admin.id,
    )
