"""Support tickets routes for cabinet."""

import math
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.cabinet.routes.websocket import notify_admins_new_ticket, notify_admins_ticket_reply
from app.config import settings
from app.database.crud.ticket_notification import TicketNotificationCRUD
from app.database.models import Ticket, TicketMessage, User
from app.handlers.tickets import notify_admins_about_new_ticket, notify_admins_about_ticket_reply

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.tickets import (
    TicketCreateRequest,
    TicketDetailResponse,
    TicketListResponse,
    TicketMessageCreateRequest,
    TicketMessageResponse,
    TicketResponse,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/tickets', tags=['Cabinet Tickets'])


def _message_to_response(message: TicketMessage) -> TicketMessageResponse:
    """Convert TicketMessage to response."""
    return TicketMessageResponse(
        id=message.id,
        message_text=message.message_text or '',
        is_from_admin=message.is_from_admin,
        has_media=bool(message.media_file_id),
        media_type=message.media_type,
        media_file_id=message.media_file_id,
        media_caption=message.media_caption,
        created_at=message.created_at,
    )


def _ticket_to_response(ticket: Ticket, include_last_message: bool = True) -> TicketResponse:
    """Convert Ticket to response."""
    last_message = None
    messages_count = len(ticket.messages) if ticket.messages else 0

    if include_last_message and ticket.messages:
        last_msg = max(ticket.messages, key=lambda m: m.created_at)
        last_message = _message_to_response(last_msg)

    return TicketResponse(
        id=ticket.id,
        title=ticket.title or f'Ticket #{ticket.id}',
        status=ticket.status,
        priority=ticket.priority or 'normal',
        created_at=ticket.created_at,
        updated_at=ticket.updated_at or ticket.created_at,
        closed_at=ticket.closed_at,
        messages_count=messages_count,
        last_message=last_message,
    )


@router.get('', response_model=TicketListResponse)
async def get_tickets(
    page: int = Query(1, ge=1, description='Page number'),
    per_page: int = Query(20, ge=1, le=100, description='Items per page'),
    status_filter: str | None = Query(None, alias='status', description='Filter by status'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get user's support tickets."""
    # Check if tickets are enabled
    if not settings.is_support_tickets_enabled():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Support tickets are disabled',
        )

    # Base query
    query = select(Ticket).where(Ticket.user_id == user.id).options(selectinload(Ticket.messages))

    # Filter by status
    if status_filter:
        query = query.where(Ticket.status == status_filter)

    # Get total count
    count_query = select(func.count()).select_from(Ticket).where(Ticket.user_id == user.id)
    if status_filter:
        count_query = count_query.where(Ticket.status == status_filter)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Paginate
    offset = (page - 1) * per_page
    query = query.order_by(desc(Ticket.updated_at)).offset(offset).limit(per_page)

    result = await db.execute(query)
    tickets = result.scalars().all()

    items = [_ticket_to_response(t) for t in tickets]
    pages = math.ceil(total / per_page) if total > 0 else 1

    return TicketListResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )


@router.post('', response_model=TicketDetailResponse)
async def create_ticket(
    request: TicketCreateRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Create a new support ticket."""
    # Check if tickets are enabled
    if not settings.is_support_tickets_enabled():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Support tickets are disabled',
        )

    # Create ticket
    ticket = Ticket(
        user_id=user.id,
        title=request.title,
        status='open',
        priority='normal',
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(ticket)
    await db.flush()

    # Create initial message with optional media
    message = TicketMessage(
        ticket_id=ticket.id,
        user_id=user.id,
        message_text=request.message,
        is_from_admin=False,
        media_type=request.media_type,
        media_file_id=request.media_file_id,
        media_caption=request.media_caption,
        created_at=datetime.now(UTC),
    )
    db.add(message)
    await db.commit()

    # Refresh to get relationships
    await db.refresh(ticket, ['messages'])

    # Уведомить админов о новом тикете (Telegram)
    try:
        await notify_admins_about_new_ticket(ticket, db)
    except Exception as e:
        logger.error('Error notifying admins about new ticket from cabinet', error=e)

    # Уведомить админов в кабинете
    try:
        notification = await TicketNotificationCRUD.create_admin_notification_for_new_ticket(db, ticket)
        if notification:
            # Отправить WebSocket уведомление
            await notify_admins_new_ticket(ticket.id, ticket.title, user.id)
    except Exception as e:
        logger.error('Error creating cabinet notification for new ticket', error=e)

    messages = [_message_to_response(m) for m in ticket.messages]

    return TicketDetailResponse(
        id=ticket.id,
        title=ticket.title,
        status=ticket.status,
        priority=ticket.priority or 'normal',
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
        closed_at=ticket.closed_at,
        is_reply_blocked=ticket.is_reply_blocked if hasattr(ticket, 'is_reply_blocked') else False,
        messages=messages,
    )


@router.get('/{ticket_id}', response_model=TicketDetailResponse)
async def get_ticket(
    ticket_id: int,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get ticket with all messages."""
    query = (
        select(Ticket).where(Ticket.id == ticket_id, Ticket.user_id == user.id).options(selectinload(Ticket.messages))
    )

    result = await db.execute(query)
    ticket = result.scalar_one_or_none()

    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Ticket not found',
        )

    messages = sorted(ticket.messages or [], key=lambda m: m.created_at)
    messages_response = [_message_to_response(m) for m in messages]

    return TicketDetailResponse(
        id=ticket.id,
        title=ticket.title or f'Ticket #{ticket.id}',
        status=ticket.status,
        priority=ticket.priority or 'normal',
        created_at=ticket.created_at,
        updated_at=ticket.updated_at or ticket.created_at,
        closed_at=ticket.closed_at,
        is_reply_blocked=ticket.is_reply_blocked if hasattr(ticket, 'is_reply_blocked') else False,
        messages=messages_response,
    )


@router.post('/{ticket_id}/messages', response_model=TicketMessageResponse)
async def add_ticket_message(
    ticket_id: int,
    request: TicketMessageCreateRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Add message to existing ticket."""
    # Get ticket
    query = select(Ticket).where(Ticket.id == ticket_id, Ticket.user_id == user.id)
    result = await db.execute(query)
    ticket = result.scalar_one_or_none()

    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Ticket not found',
        )

    # Check if ticket is closed
    if ticket.status == 'closed':
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Cannot add message to closed ticket',
        )

    # Check if replies are blocked
    if hasattr(ticket, 'is_reply_blocked') and ticket.is_reply_blocked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Replies to this ticket are blocked',
        )

    # Create message with optional media
    message = TicketMessage(
        ticket_id=ticket.id,
        user_id=user.id,
        message_text=request.message,
        is_from_admin=False,
        media_type=request.media_type,
        media_file_id=request.media_file_id,
        media_caption=request.media_caption,
        created_at=datetime.now(UTC),
    )
    db.add(message)

    # Update ticket status and timestamp
    if ticket.status == 'answered':
        ticket.status = 'pending'
    ticket.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(message)

    # Уведомить админов об ответе пользователя (Telegram)
    try:
        await notify_admins_about_ticket_reply(
            ticket,
            request.message,
            db,
            media_file_id=request.media_file_id,
            media_type=request.media_type,
        )
    except Exception as e:
        logger.error('Error notifying admins about ticket reply from cabinet', error=e)

    # Уведомить админов в кабинете
    try:
        notification = await TicketNotificationCRUD.create_admin_notification_for_user_reply(
            db, ticket, request.message
        )
        if notification:
            # Отправить WebSocket уведомление
            await notify_admins_ticket_reply(ticket.id, (request.message or '')[:100], user.id)
    except Exception as e:
        logger.error('Error creating cabinet notification for user reply', error=e)

    return _message_to_response(message)
