"""Admin cabinet API for wholesale coupon batches (RBAC: coupons:*)."""

from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.coupon import (
    create_coupon_batch,
    get_batch_coupon_tokens,
    get_batch_status_counts,
    get_coupon_batch_by_id,
    get_coupon_batches,
    get_coupon_batches_count,
    get_status_counts_for_batches,
    revoke_batch_coupons,
)
from app.database.crud.tariff import get_tariff_by_id
from app.database.models import CouponBatch, CouponStatus, User
from app.services.coupon_service import build_coupon_deeplink

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.coupons import (
    CouponBatchCreatedResponse,
    CouponBatchCreateRequest,
    CouponBatchLinksResponse,
    CouponBatchListResponse,
    CouponBatchResponse,
    CouponBatchRevokeResponse,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/coupons', tags=['Admin Coupons'])


def _build_links(tokens: list[str]) -> list[str]:
    """Deep links for the tokens; empty when the bot username is not synced yet."""
    bot_username = settings.get_bot_username()
    if not bot_username:
        return []
    return [build_coupon_deeplink(bot_username, token) for token in tokens]


def _serialize_batch(batch: CouponBatch, counts: dict[str, int]) -> CouponBatchResponse:
    return CouponBatchResponse(
        id=batch.id,
        name=batch.name,
        tariff_id=batch.tariff_id,
        tariff_name=batch.tariff.name if batch.tariff else None,
        period_days=batch.period_days,
        coupons_total=batch.coupons_total,
        wholesale_price_kopeks=batch.wholesale_price_kopeks,
        valid_until=batch.valid_until,
        is_revoked=batch.is_revoked,
        created_at=batch.created_at,
        active_count=counts.get(CouponStatus.ACTIVE.value, 0),
        redeemed_count=counts.get(CouponStatus.REDEEMED.value, 0),
        revoked_count=counts.get(CouponStatus.REVOKED.value, 0),
    )


async def _get_batch_or_404(db: AsyncSession, batch_id: int) -> CouponBatch:
    batch = await get_coupon_batch_by_id(db, batch_id)
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Coupon batch not found')
    return batch


@router.get('', response_model=CouponBatchListResponse)
async def list_coupon_batches(
    admin: User = Depends(require_permission('coupons:read')),
    db: AsyncSession = Depends(get_cabinet_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> CouponBatchListResponse:
    """List coupon batches with redemption stats."""
    total = await get_coupon_batches_count(db)
    batches = await get_coupon_batches(db, offset=offset, limit=limit)
    counts_by_batch = await get_status_counts_for_batches(db, [batch.id for batch in batches])

    return CouponBatchListResponse(
        items=[_serialize_batch(batch, counts_by_batch.get(batch.id, {})) for batch in batches],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post('', response_model=CouponBatchCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_coupon_batch_endpoint(
    payload: CouponBatchCreateRequest,
    admin: User = Depends(require_permission('coupons:create')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> CouponBatchCreatedResponse:
    """Create a batch of one-time coupons and return the generated links."""
    name = payload.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Batch name must not be blank')

    tariff = await get_tariff_by_id(db, payload.tariff_id)
    if not tariff or not tariff.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Tariff not found or inactive')

    valid_until = datetime.now(UTC) + timedelta(days=payload.valid_days) if payload.valid_days else None

    batch = await create_coupon_batch(
        db,
        name=name,
        tariff_id=tariff.id,
        period_days=payload.period_days,
        coupons_count=payload.coupons_count,
        wholesale_price_kopeks=payload.wholesale_price_kopeks,
        valid_until=valid_until,
        created_by=admin.id,
    )

    logger.info(
        'Создана партия купонов (cabinet)',
        batch_id=batch.id,
        tariff_id=tariff.id,
        count=payload.coupons_count,
        created_by=admin.id,
    )

    tokens = await get_batch_coupon_tokens(db, batch.id, status=CouponStatus.ACTIVE.value)
    counts = await get_batch_status_counts(db, batch.id)
    base = _serialize_batch(batch, counts)
    return CouponBatchCreatedResponse(**base.model_dump(), links=_build_links(tokens), tokens=tokens)


@router.get('/{batch_id}', response_model=CouponBatchResponse)
async def get_coupon_batch(
    batch_id: int,
    admin: User = Depends(require_permission('coupons:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> CouponBatchResponse:
    """Batch card with redemption stats."""
    batch = await _get_batch_or_404(db, batch_id)
    counts = await get_batch_status_counts(db, batch.id)
    return _serialize_batch(batch, counts)


@router.get('/{batch_id}/links', response_model=CouponBatchLinksResponse)
async def export_coupon_batch_links(
    batch_id: int,
    admin: User = Depends(require_permission('coupons:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> CouponBatchLinksResponse:
    """Still-active coupon links of the batch (for handing to the partner)."""
    batch = await _get_batch_or_404(db, batch_id)
    tokens = await get_batch_coupon_tokens(db, batch.id, status=CouponStatus.ACTIVE.value)
    return CouponBatchLinksResponse(
        batch_id=batch.id,
        count=len(tokens),
        links=_build_links(tokens),
        tokens=tokens,
    )


@router.post('/{batch_id}/revoke', response_model=CouponBatchRevokeResponse)
async def revoke_coupon_batch(
    batch_id: int,
    admin: User = Depends(require_permission('coupons:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> CouponBatchRevokeResponse:
    """Revoke all still-active coupons of the batch (e.g. the partner did not pay)."""
    batch = await _get_batch_or_404(db, batch_id)
    revoked_count = await revoke_batch_coupons(db, batch)

    logger.info(
        'Партия купонов отозвана (cabinet)',
        batch_id=batch.id,
        revoked_count=revoked_count,
        admin_id=admin.id,
    )

    counts = await get_batch_status_counts(db, batch.id)
    return CouponBatchRevokeResponse(revoked_count=revoked_count, batch=_serialize_batch(batch, counts))
