"""
Forum Service — manages Telegram Forum topics and ticket persistence.

Handles creating topics, saving messages, updating ticket status,
and querying tickets for the AI context window.
"""

from datetime import UTC, datetime
from typing import Sequence

import structlog
from aiogram import Bot
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models_ai_ticket import (
    AIFaqArticle,
    ForumTicket,
    ForumTicketMessage,
)

logger = structlog.get_logger(__name__)


class ForumService:
    """Encapsulates all ticket/topic lifecycle operations."""

    @staticmethod
    async def get_or_create_ticket(
        db: AsyncSession,
        bot: Bot,
        user_id: int,
        user_display_name: str,
    ) -> ForumTicket:
        """
        Get an existing open ticket for the user, or create a new one
        (including a Telegram Forum topic in the manager group).
        """
        # Check for existing open ticket
        stmt = select(ForumTicket).where(
            ForumTicket.user_id == user_id,
            ForumTicket.status == 'open',
        )
        result = await db.execute(stmt)
        ticket = result.scalars().first()
        if ticket:
            return ticket

        # Create a new Forum topic in the manager group
        from app.services.system_settings_service import BotConfigurationService
        forum_group_id = BotConfigurationService.get_current_value('SUPPORT_AI_FORUM_ID')
        
        if not forum_group_id:
            raise ValueError('SUPPORT_AI_FORUM_ID is not configured in settings')

        topic_name = f'🎫 {user_display_name} (ID: {user_id})'
        logger.info('forum_service.creating_topic', chat_id=forum_group_id, topic_name=topic_name, user_id=user_id)
        try:
            topic = await bot.create_forum_topic(
                chat_id=int(forum_group_id),
                name=topic_name[:128],  # Telegram limit
            )
            topic_id = topic.message_thread_id
        except Exception as e:
            logger.error('forum_service.create_topic_failed', error=str(e), user_id=user_id)
            raise

        # Persist the ticket
        ticket = ForumTicket(
            user_id=user_id,
            telegram_topic_id=topic_id,
            status='open',
            ai_enabled=True,
        )
        db.add(ticket)
        await db.flush()
        logger.info('forum_service.ticket_created', ticket_id=ticket.id, topic_id=topic_id, user_id=user_id)
        return ticket

    @staticmethod
    async def save_message(
        db: AsyncSession,
        ticket_id: int,
        role: str,
        content: str,
        message_id: int | None = None,
    ) -> ForumTicketMessage:
        """Save a message to the ticket history."""
        msg = ForumTicketMessage(
            ticket_id=ticket_id,
            role=role,
            content=content,
            message_id=message_id,
        )
        db.add(msg)
        await db.flush()
        return msg

    @staticmethod
    async def get_conversation_history(
        db: AsyncSession,
        ticket_id: int,
        limit: int = 20,
    ) -> list[dict[str, str]]:
        """
        Get recent messages for the ticket, formatted for AI context.
        Returns list of {'role': 'user'|'assistant', 'content': '...'}.
        """
        stmt = (
            select(ForumTicketMessage)
            .where(ForumTicketMessage.ticket_id == ticket_id)
            .order_by(ForumTicketMessage.created_at.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        messages = list(reversed(result.scalars().all()))

        history: list[dict[str, str]] = []
        for msg in messages:
            if msg.role == 'user':
                history.append({'role': 'user', 'content': msg.content})
            elif msg.role == 'ai':
                history.append({'role': 'assistant', 'content': msg.content})
            # manager and system messages are not included in AI context
        return history

    @staticmethod
    async def disable_ai(db: AsyncSession, ticket_id: int) -> None:
        """Disable AI for a ticket (e.g., manager replied or user requested)."""
        stmt = update(ForumTicket).where(ForumTicket.id == ticket_id).values(ai_enabled=False)
        await db.execute(stmt)
        logger.info('forum_service.ai_disabled', ticket_id=ticket_id)

    @staticmethod
    async def enable_ai(db: AsyncSession, ticket_id: int) -> None:
        """Re-enable AI for a ticket."""
        stmt = update(ForumTicket).where(ForumTicket.id == ticket_id).values(ai_enabled=True)
        await db.execute(stmt)

    @staticmethod
    async def close_ticket(db: AsyncSession, ticket_id: int, bot: Bot | None = None) -> None:
        """Close a ticket and its associated Forum topic."""
        # Fetch ticket to get topic_id
        stmt_select = select(ForumTicket).where(ForumTicket.id == ticket_id)
        result = await db.execute(stmt_select)
        ticket = result.scalars().first()
        
        if not ticket:
            return

        # Update DB status
        stmt_update = (
            update(ForumTicket)
            .where(ForumTicket.id == ticket_id)
            .values(status='closed', closed_at=datetime.now(UTC))
        )
        await db.execute(stmt_update)
        
        # Close Telegram Forum topic if bot is available
        if bot and ticket.telegram_topic_id:
            from app.services.system_settings_service import BotConfigurationService
            forum_group_id = BotConfigurationService.get_current_value('SUPPORT_AI_FORUM_ID')
            if forum_group_id:
                try:
                    await bot.close_forum_topic(
                        chat_id=int(forum_group_id),
                        message_thread_id=ticket.telegram_topic_id
                    )
                    logger.info('forum_service.topic_closed', chat_id=forum_group_id, topic_id=ticket.telegram_topic_id)
                except Exception as e:
                    logger.error('forum_service.close_topic_failed', error=str(e), ticket_id=ticket_id)

        logger.info('forum_service.ticket_closed_db', ticket_id=ticket_id)

    @staticmethod
    async def get_ticket_by_topic_id(db: AsyncSession, topic_id: int) -> ForumTicket | None:
        """Find an open ticket by its Forum topic ID."""
        stmt = select(ForumTicket).where(
            ForumTicket.telegram_topic_id == topic_id,
            ForumTicket.status == 'open',
        )
        result = await db.execute(stmt)
        return result.scalars().first()

    @staticmethod
    async def get_active_faq_articles(db: AsyncSession) -> Sequence[AIFaqArticle]:
        """Get all active FAQ articles for AI context injection."""
        stmt = select(AIFaqArticle).where(AIFaqArticle.is_active == True).order_by(AIFaqArticle.id)  # noqa: E712
        result = await db.execute(stmt)
        return result.scalars().all()

    @staticmethod
    def format_faq_context(articles: Sequence[AIFaqArticle]) -> str:
        """Format FAQ articles into a text block for the AI system prompt."""
        if not articles:
            return ''
        parts: list[str] = []
        for article in articles:
            parts.append(f'### {article.title}\n{article.content}')
        return '\n\n'.join(parts)

    @staticmethod
    async def count_user_tickets(db: AsyncSession, user_id: int, statuses: list[str] | None = None) -> int:
        """Count ForumTickets for a user."""
        from sqlalchemy import func
        stmt = select(func.count()).select_from(ForumTicket).where(ForumTicket.user_id == user_id)
        if statuses:
            stmt = stmt.where(ForumTicket.status.in_(statuses))
        result = await db.execute(stmt)
        return int(result.scalar() or 0)

    @staticmethod
    async def get_user_tickets(
        db: AsyncSession, user_id: int, statuses: list[str] | None = None, limit: int = 10, offset: int = 0
    ) -> list[ForumTicket]:
        """Fetch ForumTickets for a user with pagination."""
        from sqlalchemy import desc
        stmt = select(ForumTicket).where(ForumTicket.user_id == user_id)
        if statuses:
            stmt = stmt.where(ForumTicket.status.in_(statuses))
        stmt = stmt.order_by(desc(ForumTicket.created_at)).limit(limit).offset(offset)
        result = await db.execute(stmt)
        return list(result.scalars().all())
