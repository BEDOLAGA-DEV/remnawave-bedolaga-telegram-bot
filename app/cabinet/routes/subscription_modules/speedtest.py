"""Speedtest: subscriber-gated node latency targets."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import get_active_subscriptions_by_user_id
from app.database.models import User
from app.services.speedtest_service import speedtest_service

from ...dependencies import get_cabinet_db, get_current_cabinet_user


logger = structlog.get_logger(__name__)
router = APIRouter()


@router.get('/nodes-latency-targets')
async def nodes_latency_targets(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    if not settings.SPEEDTEST_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Speedtest disabled')
    subs = await get_active_subscriptions_by_user_id(db, user.id)
    if not subs:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Subscription required')
    targets = await speedtest_service.get_ping_targets()
    return {'targets': targets, 'samples': settings.get_speedtest_samples()}
