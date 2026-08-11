"""Support tickets routes for cabinet."""

import math
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.cabinet.routes.media import make_media_token
from app.cabinet.routes.websocket import notify_admins_new_ticket, notify_admins_ticket_reply
from app.database.crud.ticket import TicketCRUD
from app.database.crud.ticket_notification import TicketNotificationCRUD
from app.database.models import Ticket, TicketMessage, User
from app.handlers.tickets import notify_admins_about_new_ticket, notify_admins_about_ticket_reply
from app.services.support_settings_service import SupportSettingsService

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.tickets import (
    TicketCreateRequest,
    TicketDetailResponse,
    TicketListResponse,
    TicketMediaItem,
    TicketMessageCreateRequest,
    TicketMessageResponse,
    TicketResponse,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/tickets', tags=['Cabinet Tickets'])

# Сентинел «вечной» блокировки из TicketCRUD.is_user_globally_blocked (datetime.max).
_PERMANENT_BLOCK_YEAR = 9999


def _ensure_tickets_enabled() -> None:
    """Отказать, если тикеты выключены (режим поддержки ``contact``).

    Режим спрашиваем у сервиса, а не у ``settings``: persisted-значение живёт в
    data/support_settings.json, а в ``settings`` оно попадает только при первой
    загрузке сервиса в процессе. До неё ``settings`` отдаёт значение из ``.env``,
    и свежеперезапущенный бот пускал бы в тикеты вопреки выключенному режиму.
    """
    if not SupportSettingsService.is_tickets_enabled():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Support tickets are disabled',
        )


async def _ensure_not_blocked(db: AsyncSession, user: User) -> None:
    """Отказать, если пользователь заблокирован в поддержке.

    Бот-путь (``app/handlers/tickets.py``) проверяет глобальную блокировку и при
    создании тикета, и при ответе; кабинет её не проверял вовсе.
    """
    blocked_until = await TicketCRUD.is_user_globally_blocked(db, user.id)
    if not blocked_until:
        return
    if blocked_until.year >= _PERMANENT_BLOCK_YEAR:
        detail = 'You are blocked from contacting support'
    else:
        detail = f'You are blocked from contacting support until {blocked_until.strftime("%d.%m.%Y %H:%M")} UTC'
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _message_to_response(message: TicketMessage) -> TicketMessageResponse:
    """Convert TicketMessage to response."""
    raw_items = getattr(message, 'media_items', None) or None
    items = None
    if raw_items:
        try:
            items = [TicketMediaItem(**it) for it in raw_items]
            for it in items:
                it.token = make_media_token(it.file_id)
        except (TypeError, KeyError, ValueError) as exc:
            logger.warning('Failed to parse media_items', message_id=message.id, error=str(exc))
            items = None
    return TicketMessageResponse(
        id=message.id,
        message_text=message.message_text or '',
        is_from_admin=message.is_from_admin,
        has_media=bool(message.media_file_id) or bool(items),
        media_type=message.media_type,
        media_file_id=message.media_file_id,
        media_token=make_media_token(message.media_file_id) if message.media_file_id else None,
        media_caption=message.media_caption,
        media_items=items,
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
    _ensure_tickets_enabled()

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
    _ensure_tickets_enabled()
    await _ensure_not_blocked(db, user)

    # Один незакрытый тикет на пользователя — паритет с бот-путём
    if await TicketCRUD.user_has_active_ticket(db, user.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='You already have an open ticket',
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

    # Resolve media payload
    items_payload = None
    primary_type = request.media_type
    primary_file_id = request.media_file_id
    primary_caption = request.media_caption
    if getattr(request, 'media_items', None):
        items_payload = [it.model_dump() for it in request.media_items]
        first = request.media_items[0]
        primary_type = first.type
        primary_file_id = first.file_id
        primary_caption = primary_caption or first.caption

    # Create initial message with optional media
    has_media = bool(primary_file_id)
    message = TicketMessage(
        ticket_id=ticket.id,
        user_id=user.id,
        message_text=request.message,
        is_from_admin=False,
        has_media=has_media,
        media_type=primary_type if has_media else None,
        media_file_id=primary_file_id if has_media else None,
        media_caption=primary_caption if has_media else None,
        media_items=items_payload,
        created_at=datetime.now(UTC),
    )
    db.add(message)
    await db.commit()

    # Refresh to get relationships
    await db.refresh(ticket, ['messages'])

    # Feed the mobile support socket bridge (event_emitter -> support_ws).
    try:
        from app.services.event_emitter import event_emitter

        await event_emitter.emit(
            'ticket.created',
            {
                'ticket_id': ticket.id,
                'user_id': user.id,
                'title': ticket.title,
                'status': ticket.status,
                'priority': ticket.priority or 'normal',
                'has_media': bool(primary_file_id),
            },
            db=db,
        )
    except Exception as error:
        logger.warning('Failed to emit ticket.created from cabinet', error=error)

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
    _ensure_tickets_enabled()

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
    _ensure_tickets_enabled()
    await _ensure_not_blocked(db, user)

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

    # Resolve media payload
    items_payload = None
    primary_type = request.media_type
    primary_file_id = request.media_file_id
    primary_caption = request.media_caption
    if getattr(request, 'media_items', None):
        items_payload = [it.model_dump() for it in request.media_items]
        first = request.media_items[0]
        primary_type = first.type
        primary_file_id = first.file_id
        primary_caption = primary_caption or first.caption

    # Create message with optional media
    has_media = bool(primary_file_id)
    message = TicketMessage(
        ticket_id=ticket.id,
        user_id=user.id,
        message_text=request.message,
        is_from_admin=False,
        has_media=has_media,
        media_type=primary_type if has_media else None,
        media_file_id=primary_file_id if has_media else None,
        media_caption=primary_caption if has_media else None,
        media_items=items_payload,
        created_at=datetime.now(UTC),
    )
    db.add(message)

    # Update ticket status and timestamp
    if ticket.status == 'answered':
        ticket.status = 'pending'
    ticket.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(message)

    # Feed the mobile support socket bridge (event_emitter -> support_ws).
    try:
        from app.services.event_emitter import event_emitter

        await event_emitter.emit(
            'ticket.message_added',
            {
                'ticket_id': ticket.id,
                'message_id': message.id,
                'user_id': user.id,
                'is_from_admin': False,
                'message_text': (request.message or '')[:200],
                'has_media': bool(primary_file_id),
                'status': ticket.status,
            },
            db=db,
        )
    except Exception as error:
        logger.warning('Failed to emit ticket.message_added from cabinet', error=error)

    # Уведомить админов об ответе пользователя (Telegram)
    try:
        await notify_admins_about_ticket_reply(
            ticket,
            request.message,
            db,
            media_file_id=primary_file_id,
            media_type=primary_type,
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
