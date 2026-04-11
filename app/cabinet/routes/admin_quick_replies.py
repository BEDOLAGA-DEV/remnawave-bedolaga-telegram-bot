"""Admin ticket quick replies routes for cabinet."""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.ticket_quick_reply import TicketQuickReplyCRUD
from app.database.models import TicketQuickReply, User

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/quick-replies', tags=['Cabinet Admin Quick Replies'])


ALLOWED_CATEGORIES: tuple[str, ...] = ('billing', 'technical', 'account', 'other')


# ============== Schemas ==============


class QuickReplyResponse(BaseModel):
    id: int
    title: str
    text: str
    category: str | None = None
    created_by: int | None = None
    created_at: datetime | None = None


class QuickReplyListResponse(BaseModel):
    items: list[QuickReplyResponse]
    total: int


class QuickReplyCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    text: str = Field(..., min_length=1, max_length=10000)
    category: str | None = Field(None, max_length=50)


class QuickReplyUpdateRequest(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=255)
    text: str | None = Field(None, min_length=1, max_length=10000)
    category: str | None = Field(None, max_length=50)


# ============== Helpers ==============


def _serialize_reply(reply: TicketQuickReply) -> QuickReplyResponse:
    return QuickReplyResponse(
        id=reply.id,
        title=reply.title,
        text=reply.text,
        category=reply.category,
        created_by=reply.created_by,
        created_at=reply.created_at,
    )


def _validate_category(category: str | None) -> str | None:
    if category is None:
        return None
    normalized = category.strip().lower()
    if not normalized:
        return None
    if normalized not in ALLOWED_CATEGORIES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f'Invalid category. Allowed: {", ".join(ALLOWED_CATEGORIES)}',
        )
    return normalized


# ============== Endpoints ==============


@router.get('', response_model=QuickReplyListResponse)
async def list_quick_replies(
    admin: User = Depends(require_permission('quick_replies:read')),
    db: AsyncSession = Depends(get_cabinet_db),
    category: str | None = Query(None),
) -> QuickReplyListResponse:
    """Get list of ticket quick replies, optionally filtered by category."""
    validated_category = _validate_category(category) if category else None
    replies = await TicketQuickReplyCRUD.get_quick_replies(db, category=validated_category)
    return QuickReplyListResponse(
        items=[_serialize_reply(r) for r in replies],
        total=len(replies),
    )


@router.get('/{reply_id}', response_model=QuickReplyResponse)
async def get_quick_reply(
    reply_id: int,
    admin: User = Depends(require_permission('quick_replies:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> QuickReplyResponse:
    """Get a single quick reply by ID."""
    result = await db.execute(
        select(TicketQuickReply).where(TicketQuickReply.id == reply_id)
    )
    reply = result.scalar_one_or_none()
    if not reply:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Quick reply not found')
    return _serialize_reply(reply)


@router.post('', response_model=QuickReplyResponse, status_code=status.HTTP_201_CREATED)
async def create_quick_reply(
    payload: QuickReplyCreateRequest,
    admin: User = Depends(require_permission('quick_replies:create')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> QuickReplyResponse:
    """Create a new ticket quick reply."""
    validated_category = _validate_category(payload.category)
    reply = await TicketQuickReplyCRUD.create_quick_reply(
        db,
        title=payload.title.strip(),
        text=payload.text.strip(),
        category=validated_category,
        created_by=admin.id,
    )
    await db.commit()
    logger.info(
        'Admin created ticket quick reply via cabinet',
        reply_id=reply.id,
        admin_id=admin.id,
    )
    return _serialize_reply(reply)


@router.patch('/{reply_id}', response_model=QuickReplyResponse)
async def update_quick_reply(
    reply_id: int,
    payload: QuickReplyUpdateRequest,
    admin: User = Depends(require_permission('quick_replies:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> QuickReplyResponse:
    """Update an existing ticket quick reply."""
    result = await db.execute(
        select(TicketQuickReply).where(TicketQuickReply.id == reply_id)
    )
    reply = result.scalar_one_or_none()
    if not reply:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Quick reply not found')

    if payload.title is not None:
        reply.title = payload.title.strip()
    if payload.text is not None:
        reply.text = payload.text.strip()
    if payload.category is not None:
        reply.category = _validate_category(payload.category)

    await db.flush()
    await db.refresh(reply)
    await db.commit()
    logger.info(
        'Admin updated ticket quick reply via cabinet',
        reply_id=reply.id,
        admin_id=admin.id,
    )
    return _serialize_reply(reply)


@router.delete('/{reply_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_quick_reply(
    reply_id: int,
    admin: User = Depends(require_permission('quick_replies:delete')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> None:
    """Delete a ticket quick reply."""
    deleted = await TicketQuickReplyCRUD.delete_quick_reply(db, reply_id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Quick reply not found')
    await db.commit()
    logger.info(
        'Admin deleted ticket quick reply via cabinet',
        reply_id=reply_id,
        admin_id=admin.id,
    )
