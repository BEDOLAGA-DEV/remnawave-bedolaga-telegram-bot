from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import TicketQuickReply


logger = structlog.get_logger(__name__)


class TicketQuickReplyCRUD:
    """CRUD operations for ticket_quick_replies table."""

    @staticmethod
    async def get_quick_replies(
        db: AsyncSession,
        category: str | None = None,
    ) -> list[TicketQuickReply]:
        stmt = select(TicketQuickReply).order_by(TicketQuickReply.id)
        if category is not None:
            stmt = stmt.where(TicketQuickReply.category == category)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def create_quick_reply(
        db: AsyncSession,
        title: str,
        text: str,
        category: str | None = None,
        created_by: int | None = None,
    ) -> TicketQuickReply:
        reply = TicketQuickReply(
            title=title,
            text=text,
            category=category,
            created_by=created_by,
        )
        db.add(reply)
        await db.flush()
        await db.refresh(reply)
        logger.info('Created ticket quick reply', reply_id=reply.id, title=title)
        return reply

    @staticmethod
    async def delete_quick_reply(db: AsyncSession, reply_id: int) -> bool:
        result = await db.execute(
            select(TicketQuickReply).where(TicketQuickReply.id == reply_id)
        )
        reply = result.scalar_one_or_none()
        if not reply:
            return False
        await db.delete(reply)
        await db.flush()
        logger.info('Deleted ticket quick reply', reply_id=reply_id)
        return True
