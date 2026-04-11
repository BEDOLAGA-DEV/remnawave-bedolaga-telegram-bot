"""User notifications inbox — list, mark as read, delete."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.user_notification import (
    delete_notification,
    get_unread_count,
    get_user_notifications,
    mark_all_as_read,
    mark_as_read,
)
from app.database.models import User, UserNotification

from ..dependencies import get_cabinet_db, get_current_cabinet_user


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/notifications', tags=['Notifications'])


class UserNotificationResponse(BaseModel):
    id: int
    category: str
    level: str
    title: str | None = None
    message: str
    action_url: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    read_at: datetime | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class UserNotificationListResponse(BaseModel):
    items: list[UserNotificationResponse]
    total: int
    limit: int
    offset: int
    unread_count: int


class UnreadCountResponse(BaseModel):
    count: int


class MarkReadResponse(BaseModel):
    success: bool
    count: int = 0


def _to_response(notification: UserNotification) -> UserNotificationResponse:
    return UserNotificationResponse(
        id=notification.id,
        category=notification.category,
        level=notification.level,
        title=notification.title,
        message=notification.message,
        action_url=notification.action_url,
        data=notification.data or {},
        read_at=notification.read_at,
        created_at=notification.created_at,
    )


@router.get('', response_model=UserNotificationListResponse)
async def list_user_notifications(
    unread_only: bool = Query(False, description='Only show unread notifications'),
    category: str | None = Query(None, description='Filter by category'),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> UserNotificationListResponse:
    """Paginated list of the current user's notifications."""
    notifications, total = await get_user_notifications(
        db,
        user.id,
        unread_only=unread_only,
        category=category,
        limit=limit,
        offset=offset,
    )
    unread = await get_unread_count(db, user.id)
    return UserNotificationListResponse(
        items=[_to_response(n) for n in notifications],
        total=total,
        limit=limit,
        offset=offset,
        unread_count=unread,
    )


@router.get('/unread-count', response_model=UnreadCountResponse)
async def get_notification_unread_count(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> UnreadCountResponse:
    """Count of unread notifications (used for bell badge)."""
    count = await get_unread_count(db, user.id)
    return UnreadCountResponse(count=count)


@router.post('/{notification_id}/read', response_model=MarkReadResponse)
async def mark_notification_read(
    notification_id: int,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> MarkReadResponse:
    """Mark a specific notification as read."""
    updated = await mark_as_read(db, notification_id, user.id)
    return MarkReadResponse(success=updated, count=1 if updated else 0)


@router.post('/mark-all-read', response_model=MarkReadResponse)
async def mark_all_notifications_read(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> MarkReadResponse:
    """Mark all user's notifications as read."""
    count = await mark_all_as_read(db, user.id)
    return MarkReadResponse(success=True, count=count)


@router.delete('/{notification_id}', response_model=MarkReadResponse)
async def delete_user_notification(
    notification_id: int,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> MarkReadResponse:
    """Delete a notification."""
    deleted = await delete_notification(db, notification_id, user.id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Notification not found',
        )
    return MarkReadResponse(success=True, count=1)
