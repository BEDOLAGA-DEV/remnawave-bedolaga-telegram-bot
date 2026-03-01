"""Admin tickets routes for cabinet."""

import math
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.cabinet.routes.websocket import notify_user_ticket_reply
from app.config import settings
from app.database.crud.ticket import TicketCRUD
from app.database.crud.ticket_notification import TicketNotificationCRUD
from app.database.models import Ticket, TicketMessage, User

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.tickets import TicketMessageResponse


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/tickets', tags=['Cabinet Admin Tickets'])


# Admin-specific schemas
class AdminTicketUserInfo(BaseModel):
    """User info for admin view."""

    id: int
    telegram_id: int | None = None  # Can be None for email-only users
    email: str | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None

    class Config:
        from_attributes = True


class AdminTicketResponse(BaseModel):
    """Ticket data for admin."""

    id: int
    title: str
    status: str
    priority: str
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    messages_count: int = 0
    user: AdminTicketUserInfo | None = None
    last_message: TicketMessageResponse | None = None

    class Config:
        from_attributes = True


class AdminTicketDetailResponse(BaseModel):
    """Ticket with all messages for admin."""

    id: int
    title: str
    status: str
    priority: str
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    is_reply_blocked: bool = False
    user: AdminTicketUserInfo | None = None
    messages: list[TicketMessageResponse] = []

    class Config:
        from_attributes = True


class AdminTicketListResponse(BaseModel):
    """Paginated ticket list for admin."""

    items: list[AdminTicketResponse]
    total: int
    page: int
    per_page: int
    pages: int


class AdminReplyRequest(BaseModel):
    """Admin reply to ticket."""

    message: str = Field(..., min_length=1, max_length=4000, description='Reply message')


class AdminStatusUpdateRequest(BaseModel):
    """Update ticket status."""

    status: str = Field(..., description='New status: open, answered, pending, closed')


class AdminPriorityUpdateRequest(BaseModel):
    """Update ticket priority."""

    priority: str = Field(..., description='New priority: low, normal, high, urgent')


class AdminStatsResponse(BaseModel):
    """Ticket statistics for admin."""

    total: int
    open: int
    pending: int
    answered: int
    closed: int


class TicketSettingsResponse(BaseModel):
    """Ticket system settings."""

    sla_enabled: bool
    sla_minutes: int
    sla_check_interval_seconds: int
    sla_reminder_cooldown_minutes: int
    support_system_mode: str  # tickets, contact, both
    # Cabinet notifications settings
    cabinet_user_notifications_enabled: bool = True
    cabinet_admin_notifications_enabled: bool = True


class TicketSettingsUpdateRequest(BaseModel):
    """Update ticket settings."""

    sla_enabled: bool | None = None
    sla_minutes: int | None = Field(None, ge=1, le=1440, description='SLA time in minutes (1-1440)')
    sla_check_interval_seconds: int | None = Field(None, ge=30, le=600, description='Check interval (30-600 seconds)')
    sla_reminder_cooldown_minutes: int | None = Field(
        None, ge=1, le=120, description='Reminder cooldown (1-120 minutes)'
    )
    support_system_mode: str | None = Field(None, description='Support mode: tickets, contact, both')
    # Cabinet notifications settings
    cabinet_user_notifications_enabled: bool | None = Field(None, description='Enable user notifications in cabinet')
    cabinet_admin_notifications_enabled: bool | None = Field(None, description='Enable admin notifications in cabinet')


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


def _user_to_info(user: User) -> AdminTicketUserInfo:
    """Convert User to admin info."""
    return AdminTicketUserInfo(
        id=user.id,
        telegram_id=user.telegram_id,
        email=user.email,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )


def _ticket_to_admin_response(ticket: Ticket, include_messages: bool = False) -> AdminTicketResponse:
    """Convert Ticket to admin response."""
    last_message = None
    messages_count = len(ticket.messages) if ticket.messages else 0

    if ticket.messages:
        last_msg = max(ticket.messages, key=lambda m: m.created_at)
        last_message = _message_to_response(last_msg)

    user_info = None
    if hasattr(ticket, 'user') and ticket.user:
        user_info = _user_to_info(ticket.user)

    return AdminTicketResponse(
        id=ticket.id,
        title=ticket.title or f'Ticket #{ticket.id}',
        status=ticket.status,
        priority=ticket.priority or 'normal',
        created_at=ticket.created_at,
        updated_at=ticket.updated_at or ticket.created_at,
        closed_at=ticket.closed_at,
        messages_count=messages_count,
        user=user_info,
        last_message=last_message,
    )


@router.get('/stats', response_model=AdminStatsResponse)
async def get_ticket_stats(
    admin: User = Depends(require_permission('tickets:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get ticket statistics."""
    # Total count
    total_result = await db.execute(select(func.count()).select_from(Ticket))
    total = total_result.scalar() or 0

    # Count by status
    statuses = {}
    for status_name in ['open', 'pending', 'answered', 'closed']:
        result = await db.execute(select(func.count()).select_from(Ticket).where(Ticket.status == status_name))
        statuses[status_name] = result.scalar() or 0

    return AdminStatsResponse(
        total=total,
        open=statuses.get('open', 0),
        pending=statuses.get('pending', 0),
        answered=statuses.get('answered', 0),
        closed=statuses.get('closed', 0),
    )


@router.get('/settings', response_model=TicketSettingsResponse)
async def get_ticket_settings(
    admin: User = Depends(require_permission('tickets:settings')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get ticket system settings."""
    from app.services.support_settings_service import SupportSettingsService

    return TicketSettingsResponse(
        sla_enabled=settings.SUPPORT_TICKET_SLA_ENABLED,
        sla_minutes=settings.SUPPORT_TICKET_SLA_MINUTES,
        sla_check_interval_seconds=settings.SUPPORT_TICKET_SLA_CHECK_INTERVAL_SECONDS,
        sla_reminder_cooldown_minutes=settings.SUPPORT_TICKET_SLA_REMINDER_COOLDOWN_MINUTES,
        support_system_mode=settings.get_support_system_mode(),
        cabinet_user_notifications_enabled=SupportSettingsService.get_cabinet_user_notifications_enabled(),
        cabinet_admin_notifications_enabled=SupportSettingsService.get_cabinet_admin_notifications_enabled(),
    )


@router.patch('/settings', response_model=TicketSettingsResponse)
async def update_ticket_settings(
    request: TicketSettingsUpdateRequest,
    admin: User = Depends(require_permission('tickets:settings')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update ticket system settings."""
    from pathlib import Path

    from app.services.support_settings_service import SupportSettingsService

    # Validate support_system_mode
    if request.support_system_mode is not None:
        mode = request.support_system_mode.strip().lower()
        if mode not in {'tickets', 'contact', 'both'}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Invalid support_system_mode. Must be: tickets, contact, or both',
            )

    # Update in-memory settings
    if request.sla_enabled is not None:
        settings.SUPPORT_TICKET_SLA_ENABLED = request.sla_enabled
    if request.sla_minutes is not None:
        settings.SUPPORT_TICKET_SLA_MINUTES = request.sla_minutes
    if request.sla_check_interval_seconds is not None:
        settings.SUPPORT_TICKET_SLA_CHECK_INTERVAL_SECONDS = request.sla_check_interval_seconds
    if request.sla_reminder_cooldown_minutes is not None:
        settings.SUPPORT_TICKET_SLA_REMINDER_COOLDOWN_MINUTES = request.sla_reminder_cooldown_minutes
    if request.support_system_mode is not None:
        SupportSettingsService.set_system_mode(request.support_system_mode.strip().lower())

    # Update cabinet notification settings
    if request.cabinet_user_notifications_enabled is not None:
        SupportSettingsService.set_cabinet_user_notifications_enabled(request.cabinet_user_notifications_enabled)
    if request.cabinet_admin_notifications_enabled is not None:
        SupportSettingsService.set_cabinet_admin_notifications_enabled(request.cabinet_admin_notifications_enabled)

    # Try to persist to .env file
    try:
        env_file = Path('.env')
        if env_file.exists():
            lines = env_file.read_text().splitlines()
            updates = {}

            if request.sla_enabled is not None:
                updates['SUPPORT_TICKET_SLA_ENABLED'] = str(request.sla_enabled).lower()
            if request.sla_minutes is not None:
                updates['SUPPORT_TICKET_SLA_MINUTES'] = str(request.sla_minutes)
            if request.sla_check_interval_seconds is not None:
                updates['SUPPORT_TICKET_SLA_CHECK_INTERVAL_SECONDS'] = str(request.sla_check_interval_seconds)
            if request.sla_reminder_cooldown_minutes is not None:
                updates['SUPPORT_TICKET_SLA_REMINDER_COOLDOWN_MINUTES'] = str(request.sla_reminder_cooldown_minutes)
            if request.support_system_mode is not None:
                updates['SUPPORT_SYSTEM_MODE'] = request.support_system_mode.strip().lower()

            new_lines = []
            updated_keys = set()

            for line in lines:
                updated = False
                for key, value in updates.items():
                    if line.startswith(f'{key}='):
                        new_lines.append(f'{key}={value}')
                        updated_keys.add(key)
                        updated = True
                        break
                if not updated:
                    new_lines.append(line)

            # Add any keys that weren't found
            for key, value in updates.items():
                if key not in updated_keys:
                    new_lines.append(f'{key}={value}')

            env_file.write_text('\n'.join(new_lines) + '\n')
            logger.info('Updated ticket settings in .env file')
    except Exception as e:
        logger.warning('Failed to update .env file', error=e)

    return TicketSettingsResponse(
        sla_enabled=settings.SUPPORT_TICKET_SLA_ENABLED,
        sla_minutes=settings.SUPPORT_TICKET_SLA_MINUTES,
        sla_check_interval_seconds=settings.SUPPORT_TICKET_SLA_CHECK_INTERVAL_SECONDS,
        sla_reminder_cooldown_minutes=settings.SUPPORT_TICKET_SLA_REMINDER_COOLDOWN_MINUTES,
        support_system_mode=settings.get_support_system_mode(),
        cabinet_user_notifications_enabled=SupportSettingsService.get_cabinet_user_notifications_enabled(),
        cabinet_admin_notifications_enabled=SupportSettingsService.get_cabinet_admin_notifications_enabled(),
    )


@router.get('', response_model=AdminTicketListResponse)
async def get_all_tickets(
    page: int = Query(1, ge=1, description='Page number'),
    per_page: int = Query(20, ge=1, le=100, description='Items per page'),
    status_filter: str | None = Query(None, alias='status', description='Filter by status'),
    priority_filter: str | None = Query(None, alias='priority', description='Filter by priority'),
    user_id: int | None = Query(None, description='Filter by user ID'),
    admin: User = Depends(require_permission('tickets:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get all tickets for admin."""
    # Base query with user relationship
    query = select(Ticket).options(selectinload(Ticket.messages), selectinload(Ticket.user))

    # Build count query
    count_query = select(func.count()).select_from(Ticket)

    # Apply filters
    if status_filter:
        query = query.where(Ticket.status == status_filter)
        count_query = count_query.where(Ticket.status == status_filter)

    if priority_filter:
        query = query.where(Ticket.priority == priority_filter)
        count_query = count_query.where(Ticket.priority == priority_filter)

    if user_id:
        query = query.where(Ticket.user_id == user_id)
        count_query = count_query.where(Ticket.user_id == user_id)

    # Get total count
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Paginate - order by updated_at desc (newest first)
    offset = (page - 1) * per_page
    query = query.order_by(desc(Ticket.updated_at)).offset(offset).limit(per_page)

    result = await db.execute(query)
    tickets = result.scalars().all()

    items = [_ticket_to_admin_response(t) for t in tickets]
    pages = math.ceil(total / per_page) if total > 0 else 1

    return AdminTicketListResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )


@router.get('/{ticket_id}', response_model=AdminTicketDetailResponse)
async def get_ticket_detail(
    ticket_id: int,
    admin: User = Depends(require_permission('tickets:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get ticket with all messages for admin."""
    query = (
        select(Ticket).where(Ticket.id == ticket_id).options(selectinload(Ticket.messages), selectinload(Ticket.user))
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

    user_info = None
    if ticket.user:
        user_info = _user_to_info(ticket.user)

    return AdminTicketDetailResponse(
        id=ticket.id,
        title=ticket.title or f'Ticket #{ticket.id}',
        status=ticket.status,
        priority=ticket.priority or 'normal',
        created_at=ticket.created_at,
        updated_at=ticket.updated_at or ticket.created_at,
        closed_at=ticket.closed_at,
        is_reply_blocked=ticket.is_reply_blocked if hasattr(ticket, 'is_reply_blocked') else False,
        user=user_info,
        messages=messages_response,
    )


@router.post('/{ticket_id}/reply', response_model=TicketMessageResponse)
async def reply_to_ticket(
    ticket_id: int,
    request: AdminReplyRequest,
    admin: User = Depends(require_permission('tickets:reply')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Reply to a ticket as admin."""
    # Get ticket
    ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False, load_user=True)

    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Ticket not found',
        )

    # Create admin message
    message = TicketMessage(
        ticket_id=ticket.id,
        user_id=ticket.user_id,
        message_text=request.message,
        is_from_admin=True,
        created_at=datetime.now(UTC),
    )
    db.add(message)

    # Update ticket status to answered
    ticket.status = 'answered'
    ticket.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(message)

    # Try to notify user via Telegram
    try:
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode

        bot = Bot(
            token=settings.BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        try:
            from app.handlers.admin.tickets import notify_user_about_ticket_reply

            await notify_user_about_ticket_reply(bot, ticket, request.message, db)
        except Exception as e:
            logger.warning('Failed to notify user about ticket reply', error=e)
        finally:
            await bot.session.close()
    except Exception as e:
        logger.warning('Failed to send Telegram notification', error=e)

    # Уведомить пользователя в кабинете
    try:
        notification = await TicketNotificationCRUD.create_user_notification_for_admin_reply(
            db, ticket, request.message
        )
        if notification:
            # Отправить WebSocket уведомление
            await notify_user_ticket_reply(ticket.user_id, ticket.id, (request.message or '')[:100])
    except Exception as e:
        logger.warning('Failed to create cabinet notification for admin reply', error=e)

    return _message_to_response(message)


@router.post('/{ticket_id}/status', response_model=AdminTicketDetailResponse)
async def update_ticket_status(
    ticket_id: int,
    request: AdminStatusUpdateRequest,
    admin: User = Depends(require_permission('tickets:close')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update ticket status."""
    allowed_statuses = {'open', 'pending', 'answered', 'closed'}
    if request.status not in allowed_statuses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Invalid status. Allowed: {", ".join(allowed_statuses)}',
        )

    query = (
        select(Ticket).where(Ticket.id == ticket_id).options(selectinload(Ticket.messages), selectinload(Ticket.user))
    )

    result = await db.execute(query)
    ticket = result.scalar_one_or_none()

    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Ticket not found',
        )

    ticket.status = request.status
    ticket.updated_at = datetime.now(UTC)
    if request.status == 'closed':
        ticket.closed_at = datetime.now(UTC)
    else:
        ticket.closed_at = None

    await db.commit()
    await db.refresh(ticket)

    messages = sorted(ticket.messages or [], key=lambda m: m.created_at)
    messages_response = [_message_to_response(m) for m in messages]

    user_info = None
    if ticket.user:
        user_info = _user_to_info(ticket.user)

    return AdminTicketDetailResponse(
        id=ticket.id,
        title=ticket.title or f'Ticket #{ticket.id}',
        status=ticket.status,
        priority=ticket.priority or 'normal',
        created_at=ticket.created_at,
        updated_at=ticket.updated_at or ticket.created_at,
        closed_at=ticket.closed_at,
        is_reply_blocked=ticket.is_reply_blocked if hasattr(ticket, 'is_reply_blocked') else False,
        user=user_info,
        messages=messages_response,
    )


@router.post('/{ticket_id}/priority', response_model=AdminTicketDetailResponse)
async def update_ticket_priority(
    ticket_id: int,
    request: AdminPriorityUpdateRequest,
    admin: User = Depends(require_permission('tickets:close')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update ticket priority."""
    allowed_priorities = {'low', 'normal', 'high', 'urgent'}
    if request.priority not in allowed_priorities:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Invalid priority. Allowed: {", ".join(allowed_priorities)}',
        )

    query = (
        select(Ticket).where(Ticket.id == ticket_id).options(selectinload(Ticket.messages), selectinload(Ticket.user))
    )

    result = await db.execute(query)
    ticket = result.scalar_one_or_none()

    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Ticket not found',
        )

    ticket.priority = request.priority
    ticket.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(ticket)

    messages = sorted(ticket.messages or [], key=lambda m: m.created_at)
    messages_response = [_message_to_response(m) for m in messages]

    user_info = None
    if ticket.user:
        user_info = _user_to_info(ticket.user)

    return AdminTicketDetailResponse(
        id=ticket.id,
        title=ticket.title or f'Ticket #{ticket.id}',
        status=ticket.status,
        priority=ticket.priority or 'normal',
        created_at=ticket.created_at,
        updated_at=ticket.updated_at or ticket.created_at,
        closed_at=ticket.closed_at,
        is_reply_blocked=ticket.is_reply_blocked if hasattr(ticket, 'is_reply_blocked') else False,
        user=user_info,
        messages=messages_response,
    )
