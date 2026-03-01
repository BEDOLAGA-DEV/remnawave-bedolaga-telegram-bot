"""Admin API for managing required channels."""

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.required_channel import (
    add_channel,
    delete_channel,
    get_all_channels,
    toggle_channel,
    update_channel,
)
from app.database.models import User
from app.services.channel_subscription_service import channel_subscription_service

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.channel import (
    ChannelCreateRequest,
    ChannelListResponse,
    ChannelResponse,
    ChannelUpdateRequest,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/channel-subscriptions', tags=['Cabinet Admin Channels'])


@router.get('', response_model=ChannelListResponse)
async def list_channels(
    db: AsyncSession = Depends(get_cabinet_db),
    _admin: User = Depends(require_permission('channels:read')),
) -> ChannelListResponse:
    channels = await get_all_channels(db)
    return ChannelListResponse(
        items=[ChannelResponse.model_validate(ch) for ch in channels],
        total=len(channels),
    )


@router.post('', response_model=ChannelResponse, status_code=201)
async def create_channel(
    data: ChannelCreateRequest,
    db: AsyncSession = Depends(get_cabinet_db),
    _admin: User = Depends(require_permission('channels:edit')),
) -> ChannelResponse:
    ch = await add_channel(
        db,
        channel_id=data.channel_id,
        channel_link=data.channel_link,
        title=data.title,
        disable_trial_on_leave=data.disable_trial_on_leave,
        disable_paid_on_leave=data.disable_paid_on_leave,
    )
    await channel_subscription_service.invalidate_channels_cache()
    return ChannelResponse.model_validate(ch)


@router.patch('/{channel_db_id}', response_model=ChannelResponse)
async def update_channel_endpoint(
    channel_db_id: int,
    data: ChannelUpdateRequest,
    db: AsyncSession = Depends(get_cabinet_db),
    _admin: User = Depends(require_permission('channels:edit')),
) -> ChannelResponse:
    update_data = data.model_dump(exclude_unset=True)
    ch = await update_channel(db, channel_db_id, **update_data)
    if not ch:
        raise HTTPException(status_code=404, detail='Channel not found')
    await channel_subscription_service.invalidate_channels_cache()
    return ChannelResponse.model_validate(ch)


@router.post('/{channel_db_id}/toggle', response_model=ChannelResponse)
async def toggle_channel_endpoint(
    channel_db_id: int,
    db: AsyncSession = Depends(get_cabinet_db),
    _admin: User = Depends(require_permission('channels:edit')),
) -> ChannelResponse:
    ch = await toggle_channel(db, channel_db_id)
    if not ch:
        raise HTTPException(status_code=404, detail='Channel not found')
    await channel_subscription_service.invalidate_channels_cache()
    return ChannelResponse.model_validate(ch)


@router.delete('/{channel_db_id}', status_code=204)
async def delete_channel_endpoint(
    channel_db_id: int,
    db: AsyncSession = Depends(get_cabinet_db),
    _admin: User = Depends(require_permission('channels:edit')),
) -> None:
    ok = await delete_channel(db, channel_db_id)
    if not ok:
        raise HTTPException(status_code=404, detail='Channel not found')
    await channel_subscription_service.invalidate_channels_cache()
