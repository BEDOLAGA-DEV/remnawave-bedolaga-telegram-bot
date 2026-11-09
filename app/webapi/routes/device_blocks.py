from __future__ import annotations

from fastapi import APIRouter, Security

from app.services.device_block_service import device_block_service

from ..dependencies import require_api_token


router = APIRouter()


@router.get('/device-blocks', tags=['device-blocks'])
async def get_device_blocks(_: object = Security(require_api_token)) -> dict:
    """Полное состояние заблокированных устройств для bootstrap-pull middleware.

    Returns the same payload the bot pushes to the middleware:
    ``{"version": N, "updated_at": "...", "devices": [...]}``.
    """
    payload = await device_block_service.get_state_payload()
    if payload is None:
        return {'version': 0, 'updated_at': '', 'devices': []}
    return payload
