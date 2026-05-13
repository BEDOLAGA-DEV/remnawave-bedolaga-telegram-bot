"""Cabinet API endpoint for subscription reissue.

POST /subscription/revoke
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.services.subscription_service import SubscriptionService

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from .helpers import resolve_subscription


logger = structlog.get_logger(__name__)

router = APIRouter()


@router.post('/revoke')
async def revoke_subscription(
    subscription_id: int | None = Query(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict:
    """Revoke and reissue subscription (generate new connection link)."""
    if not settings.is_subscription_revoke_enabled():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Subscription reissue is not available',
        )

    # Reload user from current session
    from app.database.crud.user import get_user_by_id

    fresh_user = await get_user_by_id(db, user.id)
    if not fresh_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='User not found')

    subscription = await resolve_subscription(db, fresh_user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Subscription not found')

    if not subscription.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Only active subscriptions can be reissued',
        )

    # Check cooldown
    if subscription.last_revoke_at:
        elapsed = (datetime.now(UTC) - subscription.last_revoke_at).total_seconds()
        cooldown = settings.SUBSCRIPTION_REVOKE_COOLDOWN_SECONDS
        if elapsed < cooldown:
            remaining = int(cooldown - elapsed)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f'Cooldown active. Try again in {remaining} seconds.',
                headers={'Retry-After': str(remaining)},
            )

    # Execute revoke
    sub_service = SubscriptionService()
    new_url = await sub_service.revoke_subscription(db, subscription)

    if not new_url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to reissue subscription',
        )

    # Update cooldown timestamp
    subscription.last_revoke_at = datetime.now(UTC)
    await db.commit()

    logger.info(
        'Subscription revoked via cabinet API',
        user_id=user.id,
        subscription_id=subscription.id,
    )

    return {
        'success': True,
        'cooldown_seconds': settings.SUBSCRIPTION_REVOKE_COOLDOWN_SECONDS,
    }
