"""Ticket notifications routes for cabinet."""

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.ticket_notification import TicketNotificationCRUD
from app.database.models import User

from ..dependencies import get_cabinet_db, get_current_cabinet_user, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/tickets/notifications', tags=['Cabinet Ticket Notifications'])
admin_router = APIRouter(prefix='/admin/tickets/notifications', tags=['Cabinet Admin Ticket Notifications'])


# Schemas
class TicketNotificationResponse(BaseModel):
    """Single ticket notification."""

    id: int
    ticket_id: int
    notification_type: str
    message: str | None = None
    is_read: bool
    created_at: datetime
    read_at: datetime | None = None

    class Config:
        from_attributes = True


class TicketNotificationListResponse(BaseModel):
    """List of ticket notifications."""

    items: list[TicketNotificationResponse]
    unread_count: int


class UnreadCountResponse(BaseModel):
    """Unread notifications count."""

    unread_count: int


# User endpoints
@router.get('', response_model=TicketNotificationListResponse)
async def get_user_notifications(
    unread_only: bool = Query(False, description='Only return unread notifications'),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get ticket notifications for current user."""
    notifications = await TicketNotificationCRUD.get_user_notifications(
        db, user.id, unread_only=unread_only, limit=limit, offset=offset
    )
    unread_count = await TicketNotificationCRUD.count_unread_user(db, user.id)

    return TicketNotificationListResponse(
        items=[TicketNotificationResponse.model_validate(n) for n in notifications],
        unread_count=unread_count,
    )


@router.get('/unread-count', response_model=UnreadCountResponse)
async def get_user_unread_count(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get unread notifications count for current user."""
    count = await TicketNotificationCRUD.count_unread_user(db, user.id)
    return UnreadCountResponse(unread_count=count)


@router.post('/{notification_id}/read')
async def mark_notification_as_read(
    notification_id: int,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Mark a notification as read."""
    # Security: Verify notification belongs to current user and is not an admin notification
    notification = await TicketNotificationCRUD.get_by_id(db, notification_id)
    if not notification:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Notification not found',
        )

    # Check ownership: notification must belong to user and not be an admin notification
    if notification.user_id != user.id or notification.is_for_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to mark this notification as read",
        )

    await TicketNotificationCRUD.mark_as_read(db, notification_id)
    return {'success': True}


@router.post('/read-all')
async def mark_all_notifications_as_read(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Mark all notifications as read for current user."""
    count = await TicketNotificationCRUD.mark_all_as_read_user(db, user.id)
    return {'success': True, 'marked_count': count}


@router.post('/ticket/{ticket_id}/read')
async def mark_ticket_notifications_as_read(
    ticket_id: int,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Mark all notifications for a specific ticket as read."""
    count = await TicketNotificationCRUD.mark_ticket_notifications_as_read(db, ticket_id, user.id, is_admin=False)
    return {'success': True, 'marked_count': count}


# Admin endpoints
@admin_router.get('', response_model=TicketNotificationListResponse)
async def get_admin_notifications(
    unread_only: bool = Query(False, description='Only return unread notifications'),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    admin: User = Depends(require_permission('tickets:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get ticket notifications for admins."""
    notifications = await TicketNotificationCRUD.get_admin_notifications(
        db, unread_only=unread_only, limit=limit, offset=offset
    )
    unread_count = await TicketNotificationCRUD.count_unread_admin(db)

    return TicketNotificationListResponse(
        items=[TicketNotificationResponse.model_validate(n) for n in notifications],
        unread_count=unread_count,
    )


@admin_router.get('/unread-count', response_model=UnreadCountResponse)
async def get_admin_unread_count(
    admin: User = Depends(require_permission('tickets:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get unread notifications count for admins."""
    count = await TicketNotificationCRUD.count_unread_admin(db)
    return UnreadCountResponse(unread_count=count)


@admin_router.post('/{notification_id}/read')
async def mark_admin_notification_as_read(
    notification_id: int,
    admin: User = Depends(require_permission('tickets:settings')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Mark an admin notification as read."""
    # Security: Verify notification exists and is an admin notification
    notification = await TicketNotificationCRUD.get_by_id(db, notification_id)
    if not notification:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Notification not found',
        )

    # Check that this is actually an admin notification
    if not notification.is_for_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='This is not an admin notification',
        )

    await TicketNotificationCRUD.mark_as_read(db, notification_id)
    return {'success': True}


@admin_router.post('/read-all')
async def mark_all_admin_notifications_as_read(
    admin: User = Depends(require_permission('tickets:settings')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Mark all admin notifications as read."""
    count = await TicketNotificationCRUD.mark_all_as_read_admin(db)
    return {'success': True, 'marked_count': count}


@admin_router.post('/ticket/{ticket_id}/read')
async def mark_admin_ticket_notifications_as_read(
    ticket_id: int,
    admin: User = Depends(require_permission('tickets:settings')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Mark all admin notifications for a specific ticket as read."""
    count = await TicketNotificationCRUD.mark_ticket_notifications_as_read(db, ticket_id, admin.id, is_admin=True)
    return {'success': True, 'marked_count': count}
