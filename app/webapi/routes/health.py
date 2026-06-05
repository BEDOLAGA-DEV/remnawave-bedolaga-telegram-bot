from __future__ import annotations

from fastapi import APIRouter, Security

from app.config import settings
from app.database import db_manager, get_pool_metrics
from app.services.version_service import version_service

from ..dependencies import require_api_token
from ..schemas.health import HealthCheckResponse, HealthFeatureFlags


router = APIRouter()


@router.get('/health', tags=['health'], response_model=HealthCheckResponse)
async def health_check() -> HealthCheckResponse:
    # Public liveness probe — Docker/LB/monitoring/cabinet healthchecks must reach
    # it WITHOUT an API token (this endpoint previously 401'd them). Only
    # non-sensitive status/version/feature flags are returned, consistent with the
    # already-public /health/unified. The detailed database/pool endpoints below
    # stay token-gated.
    return HealthCheckResponse(
        status='ok',
        api_version=settings.WEB_API_VERSION,
        bot_version=version_service.current_version,
        features=HealthFeatureFlags(
            monitoring=settings.MONITORING_INTERVAL > 0,
            maintenance=True,
            reporting=True,
            webhooks=bool(settings.WEBHOOK_URL),
        ),
    )


@router.get('/health/database', tags=['health'])
async def database_health(_: object = Security(require_api_token)) -> dict:
    """Детальная информация о состоянии базы данных."""

    return await db_manager.health_check()


@router.get('/metrics/pool', tags=['health'])
async def pool_metrics(_: object = Security(require_api_token)) -> dict:
    """Метрики пула подключений к базе данных."""

    return await get_pool_metrics()
