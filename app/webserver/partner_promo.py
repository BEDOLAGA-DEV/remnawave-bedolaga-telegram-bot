from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import RedirectResponse

from app.config import settings
from app.database.crud import partner_promo as crud
from app.database.database import AsyncSessionLocal


logger = structlog.get_logger(__name__)
router = APIRouter(prefix='/partner-promo', tags=['Partner Promo'])


@router.get('/{promo_id}/go')
async def partner_promo_go(promo_id: int):
    if not settings.PARTNER_SHOWCASE_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Not found')
    async with AsyncSessionLocal() as db:
        promo = await crud.get(db, promo_id)
        if promo is None or not promo.is_active:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Not found')
        await crud.increment_click(db, promo_id)
    return RedirectResponse(url=promo.url, status_code=status.HTTP_302_FOUND)
