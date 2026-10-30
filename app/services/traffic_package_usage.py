"""Read panel usage without committing, resetting traffic or changing a subscription."""

import asyncio

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Subscription


logger = structlog.get_logger(__name__)


async def read_panel_used_gb(db: AsyncSession, subscription: Subscription) -> float | None:
    from app.services.subscription_service import SubscriptionService

    service = SubscriptionService()
    if settings.is_multi_tariff_enabled():
        panel_user_id = getattr(subscription, 'remnawave_id', None)
    else:
        user = await get_user_by_id(db, subscription.user_id)
        panel_user_id = getattr(user, 'remnawave_id', None) if user else None
    if panel_user_id is None:
        return None

    try:
        async with asyncio.timeout(10), service.get_api_client() as api:
            panel_user = await api.get_user_by_id(panel_user_id)
    except Exception as exc:
        logger.warning(
            'Не удалось получить актуальный расход трафика из панели',
            subscription_id=subscription.id,
            user_id=subscription.user_id,
            error=exc,
        )
        return None

    if panel_user is None:
        return None
    return service._bytes_to_gb(panel_user.used_traffic_bytes)
