"""Cabinet editor API for bot text and custom-emoji presentation overrides."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.system_setting import delete_system_setting, upsert_system_setting
from app.database.models import User
from app.services.bot_presentation_catalog import (
    build_bot_presentation_catalog,
    validate_config_against_catalog,
)
from app.services.bot_presentation_service import (
    BOT_PRESENTATION_KEY,
    MAX_EMOJI_OVERRIDES,
    MAX_TEXT_OVERRIDES,
    BotPresentationConfig,
    clear_bot_presentation_cache,
    get_bot_presentation_config,
    set_bot_presentation_cache,
)

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)
router = APIRouter(prefix='/admin/bot-presentation', tags=['Admin Bot Presentation'])


class PresentationConfigPayload(BaseModel):
    emoji_overrides: dict[str, str] = Field(default_factory=dict, max_length=MAX_EMOJI_OVERRIDES)
    text_overrides: dict[str, str] = Field(default_factory=dict, max_length=MAX_TEXT_OVERRIDES)


class PresentationConfigResponse(PresentationConfigPayload):
    version: int = 2
    emoji_catalog_count: int
    text_catalog_count: int


class CatalogResponse(BaseModel):
    kind: Literal['emoji', 'text']
    total: int
    offset: int
    limit: int
    items: list[dict[str, Any]]


async def _config_response() -> PresentationConfigResponse:
    config = get_bot_presentation_config()
    catalog = await asyncio.to_thread(build_bot_presentation_catalog)
    return PresentationConfigResponse(
        version=config.version,
        emoji_overrides=config.emoji_overrides,
        text_overrides=config.text_overrides,
        emoji_catalog_count=len(catalog.emoji),
        text_catalog_count=len(catalog.texts),
    )


@router.get('', response_model=PresentationConfigResponse)
async def get_bot_presentation_config_route(
    _admin: User = Depends(require_permission('settings:read')),
):
    return await _config_response()


@router.get('/catalog', response_model=CatalogResponse)
async def get_bot_presentation_catalog_route(
    kind: Literal['emoji', 'text'],
    query: str = Query(default='', max_length=200),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    _admin: User = Depends(require_permission('settings:read')),
):
    catalog = await asyncio.to_thread(build_bot_presentation_catalog)
    config = get_bot_presentation_config()
    needle = query.strip().casefold()

    if kind == 'emoji':
        items = [
            {
                'token': item.token,
                'localization_key': item.localization_key,
                'occurrence': item.occurrence,
                'glyph': item.glyph,
                'custom_emoji_id': config.emoji_overrides.get(item.token, ''),
                'usage_count': item.usage_count,
                'usages': item.usages,
            }
            for item in catalog.emoji.values()
            if not needle
            or needle in item.token.casefold()
            or needle in item.glyph.casefold()
            or needle in config.emoji_overrides.get(item.token, '').casefold()
            or any(needle in usage.casefold() for usage in item.usages)
        ]
    else:
        items = [
            {
                'key': item.key,
                'default': item.default,
                'override': config.text_overrides.get(item.key, ''),
                'usage_count': item.usage_count,
                'usages': item.usages,
            }
            for item in catalog.texts.values()
            if not needle
            or needle in item.key.casefold()
            or needle in item.default.casefold()
            or needle in config.text_overrides.get(item.key, '').casefold()
        ]

    return CatalogResponse(
        kind=kind,
        total=len(items),
        offset=offset,
        limit=limit,
        items=items[offset : offset + limit],
    )


@router.put('', response_model=PresentationConfigResponse)
async def update_bot_presentation_config_route(
    payload: PresentationConfigPayload,
    admin: User = Depends(require_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    config = BotPresentationConfig(
        emoji_overrides={key: value.strip() for key, value in payload.emoji_overrides.items() if value.strip()},
        text_overrides={key: value for key, value in payload.text_overrides.items() if value.strip()},
    )
    try:
        await asyncio.to_thread(validate_config_against_catalog, config)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error

    await upsert_system_setting(
        db,
        BOT_PRESENTATION_KEY,
        json.dumps(config.to_raw(), ensure_ascii=False),
        description='Russian bot text and Telegram custom emoji presentation overrides',
    )
    await db.commit()
    set_bot_presentation_cache(config)
    logger.info(
        'Admin updated bot presentation overrides',
        telegram_id=admin.telegram_id,
        emoji_overrides=len(config.emoji_overrides),
        text_overrides=len(config.text_overrides),
    )
    return await _config_response()


@router.post('/reset', response_model=PresentationConfigResponse)
async def reset_bot_presentation_config_route(
    admin: User = Depends(require_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    await delete_system_setting(db, BOT_PRESENTATION_KEY)
    await db.commit()
    clear_bot_presentation_cache()
    logger.info('Admin reset bot presentation overrides', telegram_id=admin.telegram_id)
    return await _config_response()
