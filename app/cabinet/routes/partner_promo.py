"""Cabinet partner promos showcase endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import partner_promo as crud

from ..dependencies import get_cabinet_db


router = APIRouter(prefix='/partner-promos', tags=['Cabinet Partner Promos'])


@router.get('')
async def list_partner_promos(db: AsyncSession = Depends(get_cabinet_db)) -> dict[str, Any]:
    """Return active partner promo cards for the cabinet showcase page.

    Gated by PARTNER_SHOWCASE_ENABLED; no user auth required (public content).
    """
    if not settings.PARTNER_SHOWCASE_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Not found')
    promos = await crud.list_active(db)
    return {
        'promos': [
            {
                'id': p.id,
                'title': p.title,
                'description': p.description,
                'image_url': p.image_url,
                'go_url': f'/partner-promo/{p.id}/go',
            }
            for p in promos
        ]
    }
