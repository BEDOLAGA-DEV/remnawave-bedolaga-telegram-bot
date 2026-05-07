"""WL traffic endpoints for cabinet."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Query as QueryParam
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from ...schemas.subscription import TrafficPackageResponse
from ._traffic_core import resolve_traffic_packages
from .helpers import resolve_subscription


logger = structlog.get_logger(__name__)
router = APIRouter()


@router.get('/wl-traffic-packages', response_model=list[TrafficPackageResponse])
async def get_wl_traffic_packages(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
) -> list[TrafficPackageResponse]:
    """Available WL top-up packages for the resolved subscription."""
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        return []

    packages = await resolve_traffic_packages(db, subscription, kind='wl')
    return [
        TrafficPackageResponse(
            gb=p['gb'],
            price_kopeks=p['price'],
            price_rubles=p['price'] / 100,
            is_unlimited=p['is_unlimited'],
        )
        for p in packages
    ]
