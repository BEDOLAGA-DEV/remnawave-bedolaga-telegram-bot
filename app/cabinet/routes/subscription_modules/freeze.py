"""Subscription freeze (vacation) endpoints: POST /freeze, POST /resume."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query as QueryParam, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.services.freeze_service import FreezeError, FreezeService

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from .helpers import resolve_subscription


logger = structlog.get_logger(__name__)
router = APIRouter()

_freeze_service = FreezeService()

_CODE_TO_STATUS = {
    'already_frozen': status.HTTP_409_CONFLICT,
    'not_frozen': status.HTTP_409_CONFLICT,
    'trial': status.HTTP_400_BAD_REQUEST,
    'daily': status.HTTP_400_BAD_REQUEST,
    'not_active': status.HTTP_400_BAD_REQUEST,
    'too_young': status.HTTP_400_BAD_REQUEST,
    'cooldown': status.HTTP_429_TOO_MANY_REQUESTS,
    'quota_exhausted': status.HTTP_400_BAD_REQUEST,
    'panel_error': status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _guard_enabled() -> None:
    if not settings.SUBSCRIPTION_FREEZE_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Freeze disabled')


@router.post('/freeze')
async def freeze_subscription(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None),
) -> dict[str, Any]:
    _guard_enabled()
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')
    try:
        await _freeze_service.freeze_subscription(db, subscription, user)
    except FreezeError as e:
        raise HTTPException(status_code=_CODE_TO_STATUS.get(e.code, 400), detail={'code': e.code, 'message': e.message})
    return {'frozen': True, 'frozen_until': subscription.frozen_until.isoformat() if subscription.frozen_until else None}


@router.post('/resume')
async def resume_subscription(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None),
) -> dict[str, Any]:
    _guard_enabled()
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')
    try:
        await _freeze_service.resume_subscription(db, subscription, user, reason='manual')
    except FreezeError as e:
        raise HTTPException(status_code=_CODE_TO_STATUS.get(e.code, 400), detail={'code': e.code, 'message': e.message})
    return {'frozen': False, 'end_date': subscription.end_date.isoformat() if subscription.end_date else None}
