"""Admin routes for managing RemnaWave app configuration."""

import re

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.services.remnawave_service import RemnaWaveService
from app.services.system_settings_service import bot_configuration_service

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/apps', tags=['Cabinet Admin Apps'])


# ============ Schemas ============


class RemnaWaveConfigStatus(BaseModel):
    """Status of RemnaWave config integration."""

    enabled: bool
    config_uuid: str | None = None


class UpdateRemnaWaveUuidRequest(BaseModel):
    """Request to update RemnaWave config UUID."""

    uuid: str | None = None


# ============ Helpers ============

_UUID_PATTERN = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')


def _get_remnawave_config_uuid() -> str | None:
    """Get RemnaWave config UUID from system settings or env."""
    try:
        return bot_configuration_service.get_current_value('CABINET_REMNA_SUB_CONFIG')
    except Exception:
        return settings.CABINET_REMNA_SUB_CONFIG


# ============ Routes ============


@router.get('/remnawave/status', response_model=RemnaWaveConfigStatus)
async def get_remnawave_config_status(
    admin: User = Depends(require_permission('apps:read')),
):
    """Get RemnaWave config integration status."""
    config_uuid = _get_remnawave_config_uuid()
    return RemnaWaveConfigStatus(
        enabled=bool(config_uuid),
        config_uuid=config_uuid,
    )


@router.put('/remnawave/uuid', response_model=RemnaWaveConfigStatus)
async def set_remnawave_config_uuid(
    request: UpdateRemnaWaveUuidRequest,
    admin: User = Depends(require_permission('apps:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Set RemnaWave subscription config UUID."""
    uuid_value = request.uuid.strip() if request.uuid else None

    if uuid_value and not _UUID_PATTERN.match(uuid_value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid UUID format',
        )

    try:
        await bot_configuration_service.set_value(db, 'CABINET_REMNA_SUB_CONFIG', uuid_value)
        await db.commit()

        from app.handlers.subscription.common import invalidate_app_config_cache

        invalidate_app_config_cache()
        logger.info('Admin updated CABINET_REMNA_SUB_CONFIG', admin_id=admin.id, uuid_value=uuid_value)
    except Exception as e:
        logger.error('Error saving RemnaWave config UUID', error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to save configuration',
        )

    return RemnaWaveConfigStatus(
        enabled=bool(uuid_value),
        config_uuid=uuid_value,
    )


@router.get('/remnawave/config')
async def get_remnawave_subscription_config(
    admin: User = Depends(require_permission('apps:read')),
):
    """Fetch subscription page config from RemnaWave panel."""
    config_uuid = _get_remnawave_config_uuid()
    if not config_uuid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='RemnaWave subscription config is not configured',
        )

    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            config = await api.get_subscription_page_config(config_uuid)
            if not config:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail='Subscription config not found',
                )

            return {
                'uuid': config.uuid,
                'name': config.name,
                'view_position': config.view_position,
                'config': config.config,
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error fetching RemnaWave config', error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to fetch config from RemnaWave',
        )


@router.get('/remnawave/configs')
async def list_remnawave_subscription_configs(
    admin: User = Depends(require_permission('apps:read')),
):
    """List available subscription page configs from RemnaWave panel."""
    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            configs = await api.get_subscription_page_configs()
            return [
                {
                    'uuid': c.uuid,
                    'name': c.name,
                    'view_position': c.view_position,
                }
                for c in configs
            ]
    except Exception as e:
        logger.error('Error listing RemnaWave configs', error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to fetch configs from RemnaWave',
        )
