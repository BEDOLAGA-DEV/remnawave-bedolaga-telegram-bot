"""Admin endpoints for broadcasting web notifications to users.

Unlike AdminBroadcasts (which sends to Telegram/Email), this module sends
notifications through the 4-layer web delivery stack:
- Persistent UserNotification records for ALL target users (inbox)
- WebSocket push to connected cabinets (instant toast)
- Web Push via VAPID for offline users (OS notification)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.routes.websocket import broadcast_admin_message_to_all
from app.database.crud.user_notification import (
    bulk_create_notifications,
    get_user_notifications,
)
from app.database.models import Subscription, SubscriptionStatus, User
from app.services.web_push_service import web_push_service

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/user-notifications', tags=['Admin User Notifications'])


# ============== Schemas ==============


class BroadcastRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    message: str = Field(..., min_length=1, max_length=2000)
    level: Literal['info', 'success', 'warning', 'error'] = 'info'
    action_url: str | None = Field(None, max_length=500)
    target: Literal['all', 'active_subscribers', 'specific_users'] = 'all'
    user_ids: list[int] | None = None


class BroadcastResponse(BaseModel):
    success: bool
    target_count: int
    websocket_delivered: int
    web_push_delivered: int


class HistoryItem(BaseModel):
    id: int
    category: str
    level: str
    title: str | None
    message: str
    action_url: str | None
    created_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class HistoryResponse(BaseModel):
    items: list[HistoryItem]
    total: int


# ============== Helpers ==============


async def _resolve_target_user_ids(
    db: AsyncSession,
    target: str,
    user_ids: list[int] | None,
) -> list[int]:
    """Resolve target selector → list of user IDs."""
    if target == 'specific_users':
        return user_ids or []

    if target == 'active_subscribers':
        # Users who have at least one active (non-trial) subscription
        result = await db.execute(
            select(User.id)
            .join(Subscription, Subscription.user_id == User.id)
            .where(Subscription.status == SubscriptionStatus.ACTIVE.value)
            .distinct()
        )
        return [row[0] for row in result.all()]

    # target == 'all'
    result = await db.execute(select(User.id))
    return [row[0] for row in result.all()]


# ============== Endpoints ==============


@router.post('/broadcast', response_model=BroadcastResponse)
async def broadcast_to_users(
    request: BroadcastRequest,
    admin: User = Depends(require_permission('user_notifications:broadcast')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BroadcastResponse:
    """Broadcast a notification to users via 3 channels:
    1. Persistent UserNotification (DB)
    2. WebSocket to all connected cabinets
    3. Web Push (VAPID) to all push-subscribed browsers
    """
    user_ids = await _resolve_target_user_ids(db, request.target, request.user_ids)

    if not user_ids:
        return BroadcastResponse(
            success=False,
            target_count=0,
            websocket_delivered=0,
            web_push_delivered=0,
        )

    # 1. Create persistent notifications for all target users
    notifications = await bulk_create_notifications(
        db,
        user_ids=user_ids,
        category='admin_broadcast',
        level=request.level,
        title=request.title,
        message=request.message,
        action_url=request.action_url,
        data={
            'broadcast_by': admin.id,
            'target': request.target,
        },
    )

    # 2. WebSocket broadcast to connected cabinets
    # Use the first notification's ID as a reference (all belong to same broadcast)
    reference_id = notifications[0].id if notifications else None
    ws_delivered = 0
    try:
        ws_delivered = await broadcast_admin_message_to_all(
            notification_id=reference_id,
            title=request.title,
            message=request.message,
            level=request.level,
            action_url=request.action_url,
        )
    except Exception as e:
        logger.warning('WebSocket broadcast failed', error=e)

    # 3. Web Push (VAPID) — only if enabled and configured
    push_delivered = 0
    if web_push_service.is_enabled:
        try:
            push_delivered = await web_push_service.send_to_all(
                db,
                title=request.title,
                body=request.message,
                url=request.action_url or '/notifications',
                level=request.level,
                tag=f'admin_broadcast_{reference_id or "general"}',
                extra_data={
                    'notification_id': reference_id,
                    'category': 'admin_broadcast',
                },
            )
        except Exception as e:
            logger.warning('Web Push broadcast failed', error=e)

    logger.info(
        'Admin broadcast sent',
        admin_id=admin.id,
        target=request.target,
        target_count=len(user_ids),
        ws_delivered=ws_delivered,
        push_delivered=push_delivered,
    )

    return BroadcastResponse(
        success=True,
        target_count=len(user_ids),
        websocket_delivered=ws_delivered,
        web_push_delivered=push_delivered,
    )


@router.get('/history', response_model=HistoryResponse)
async def get_broadcast_history(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    admin: User = Depends(require_permission('user_notifications:broadcast')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> HistoryResponse:
    """Get history of admin broadcasts (deduplicated by admin_broadcast category).

    Since a single broadcast creates many UserNotification rows (one per user),
    we return representative rows from the admin's own notifications.
    """
    # Return recent admin_broadcast notifications the requesting admin sent to themselves
    notifications, total = await get_user_notifications(
        db,
        admin.id,
        category='admin_broadcast',
        limit=limit,
        offset=offset,
    )
    return HistoryResponse(
        items=[
            HistoryItem(
                id=n.id,
                category=n.category,
                level=n.level,
                title=n.title,
                message=n.message,
                action_url=n.action_url,
                created_at=n.created_at,
                data=n.data or {},
            )
            for n in notifications
        ],
        total=total,
    )
