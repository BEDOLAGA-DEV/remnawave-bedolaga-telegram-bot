"""Web Push (VAPID) subscription management endpoints.

Users register their browser push subscription here after they grant notification
permission in the browser. The backend stores subscriptions and uses them to
deliver push notifications via pywebpush.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.web_push_subscription import (
    deactivate_by_endpoint,
    upsert_subscription,
)
from app.database.models import User
from app.services.web_push_service import web_push_service

from ..dependencies import get_cabinet_db, get_current_cabinet_user


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/web-push', tags=['Web Push'])


# ============== Schemas ==============


class VapidKeyResponse(BaseModel):
    public_key: str
    enabled: bool


class PushKeys(BaseModel):
    p256dh: str = Field(..., min_length=1, max_length=255)
    auth: str = Field(..., min_length=1, max_length=255)


class SubscribeRequest(BaseModel):
    endpoint: str = Field(..., min_length=1)
    keys: PushKeys


class UnsubscribeRequest(BaseModel):
    endpoint: str = Field(..., min_length=1)


class PushActionResponse(BaseModel):
    success: bool
    subscription_id: int | None = None


class PushTestRequest(BaseModel):
    title: str = 'Test notification'
    body: str = 'If you see this, Web Push works!'


# ============== Endpoints ==============


@router.get('/vapid-key', response_model=VapidKeyResponse)
async def get_vapid_public_key() -> VapidKeyResponse:
    """Return the VAPID public key needed for browser subscription.

    This key is public by design — it's used to create the push subscription
    in the browser via `pushManager.subscribe({applicationServerKey: ...})`.
    """
    return VapidKeyResponse(
        public_key=web_push_service.public_key,
        enabled=web_push_service.is_enabled,
    )


@router.post('/subscribe', response_model=PushActionResponse)
async def subscribe_to_web_push(
    request_body: SubscribeRequest,
    request: Request,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PushActionResponse:
    """Register a new browser push subscription for the current user.

    Called by frontend after `pushManager.subscribe()` succeeds in the browser.
    """
    if not web_push_service.is_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Web Push is not enabled on this server',
        )

    user_agent = request.headers.get('user-agent', '')[:500] or None

    subscription = await upsert_subscription(
        db,
        user_id=user.id,
        endpoint=request_body.endpoint,
        p256dh=request_body.keys.p256dh,
        auth=request_body.keys.auth,
        user_agent=user_agent,
    )

    logger.info(
        'Web Push subscription registered',
        user_id=user.id,
        subscription_id=subscription.id,
    )

    return PushActionResponse(success=True, subscription_id=subscription.id)


@router.post('/unsubscribe', response_model=PushActionResponse)
async def unsubscribe_from_web_push(
    request_body: UnsubscribeRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PushActionResponse:
    """Remove a browser push subscription."""
    deactivated = await deactivate_by_endpoint(db, user.id, request_body.endpoint)
    return PushActionResponse(success=deactivated)


@router.post('/test', response_model=PushActionResponse)
async def send_test_push(
    request_body: PushTestRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PushActionResponse:
    """Send a test push to the current user's own subscriptions.

    Useful for verifying that VAPID keys, service worker, and push service
    all work together.
    """
    if not web_push_service.is_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Web Push is not enabled',
        )

    delivered = await web_push_service.send_to_user(
        db,
        user.id,
        title=request_body.title,
        body=request_body.body,
        url='/notifications',
        tag='test',
    )
    return PushActionResponse(success=delivered > 0)
