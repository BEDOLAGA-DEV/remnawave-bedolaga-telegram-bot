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
from sqlalchemy.orm import joinedload
from app.database.models import User, Subscription, Tariff, Transaction, SubscriptionStatus, PromoGroup
from app.database.crud.transaction import get_user_total_spent_kopeks
from app.database.crud.promo_group import get_auto_assign_promo_groups
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
            logger.error('forum_service.no_forum_id', user_id=user_id)
            return None

        topic_name = f'🎫 {user_display_name} (ID: {user_id})'
        logger.info('forum_service.creating_topic', chat_id=forum_group_id, topic_name=topic_name, user_id=user_id)
        try:
            topic = await bot.create_forum_topic(
                chat_id=int(forum_group_id),
                name=topic_name[:128],  # Telegram limit
            )
            topic_id = topic.message_thread_id
        except Exception as e:
            logger.error('forum_service.create_topic_failed', error=str(e), user_id=user_id, forum_id=forum_group_id)
            return None

        # Persist the ticket
        ai_enabled_global = BotConfigurationService.get_current_value('SUPPORT_AI_ENABLED')
        if isinstance(ai_enabled_global, str):
            ai_enabled_global = ai_enabled_global.lower() in ('true', '1', 'on', 'yes')
            
        ticket = ForumTicket(
            user_id=user_id,
            telegram_topic_id=topic_id,
            status='open',
            ai_enabled=bool(ai_enabled_global),
        )
        db.add(ticket)
        await db.flush()
        logger.info('forum_service.ticket_created', 
                    ticket_id=ticket.id, 
                    topic_id=topic_id, 
                    user_id=user_id, 
                    ai_enabled=ticket.ai_enabled)
        return ticket

    @staticmethod
    async def save_message(
        db: AsyncSession,
        ticket_id: int,
        role: str,
        content: str,
        message_id: int | None = None,
        media_type: str | None = None,
        media_file_id: str | None = None,
    ) -> ForumTicketMessage:
        """Сохранить сообщение в историю тикета (с опциональным медиа)."""
        msg = ForumTicketMessage(
            ticket_id=ticket_id,
            role=role,
            content=content,
            message_id=message_id,
            media_type=media_type,
            media_file_id=media_file_id,
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
        """Format FAQ articles into a text block for the AI system prompt.
        Включает информацию о медиа-вложениях (теги [MEDIA:tag]) для каждой статьи.
        """
        if not articles:
            return ''
        parts: list[str] = []
        for article in articles:
            block = f'### {article.title}\n{article.content}'
            # Добавляем информацию о медиа-вложениях
            media_items = getattr(article, 'media', None) or []
            if media_items:
                media_lines = []
                for m in media_items:
                    desc = m.caption or m.media_type
                    # Используем формат, который ИИ сложнее будет перепутать с основным ответом
                    media_lines.append(f'- [MEDIA:{m.tag}] ({desc})')
                block += '\n📎 Доступные медиа-теги для вставки (если полезно):\n' + '\n'.join(media_lines)
            parts.append(block)
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
    ) -> Sequence[ForumTicket]:
        """Get ForumTickets for a user with pagination."""
        stmt = select(ForumTicket).where(ForumTicket.user_id == user_id)
        if statuses:
            stmt = stmt.where(ForumTicket.status.in_(statuses))
        stmt = stmt.order_by(ForumTicket.created_at.desc()).offset(offset).limit(limit)
        result = await db.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def get_rich_user_context(db: AsyncSession, user_id: int) -> str:
        """
        Собирает максимально детальную информацию о профиле пользователя для ИИ.
        Включает тариф, подписку, баланс, лимиты и реферальные данные.
        """
        # 1. Получаем пользователя со всеми связями
        stmt = (
            select(User)
            .options(
                joinedload(User.subscription).joinedload(Subscription.tariff),
                joinedload(User.user_promo_groups),
            )
            .where(User.id == user_id)
        )
        res = await db.execute(stmt)
        user = res.scalars().first()
        if not user:
            return f"ID Пользователя: {user_id} (данные не найдены)"

        parts: list[str] = []
        parts.append(f"👤 КЛИЕНТ (ID: {user.id})")
        parts.append(f"Имя: {user.full_name}")
        parts.append(f"Username: @{user.username}" if user.username else "Username: отсутствует")
        parts.append(f"Язык: {user.language}")
        parts.append(f"Баланс: {user.balance_rubles:.2f} руб.")
        parts.append(f"Дата регистрации: {user.created_at.strftime('%Y-%m-%d') if user.created_at else 'неизвестна'}")

        # 2. Подписка и тариф
        sub = user.subscription
        if sub:
            parts.append(f"\n💎 ТЕКУЩАЯ ПОДПИСКА:")
            parts.append(f"Статус: {sub.status_display} ({sub.actual_status})")
            
            if sub.tariff:
                parts.append(f"Тариф: {sub.tariff.name}")
                if sub.tariff.description:
                    parts.append(f"Описание тарифа: {sub.tariff.description}")
            else:
                parts.append("Тариф: не назначен")

            if sub.end_date:
                parts.append(f"Действует до: {sub.end_date.strftime('%Y-%m-%d %H:%M')}")
                parts.append(f"Осталось дней: {sub.days_left}")

            # Трафик
            if sub.traffic_limit_gb > 0:
                used = sub.traffic_used_gb or 0.0
                parts.append(f"Трафик: {used:.2f} ГБ / {sub.traffic_limit_gb} ГБ ({(used / sub.traffic_limit_gb) * 100 if sub.traffic_limit_gb > 0 else 0:.1f}%)")
            else:
                parts.append("Трафик: Безлимитный")

            # Устройства
            parts.append(f"Лимит устройств по тарифу: {sub.device_limit}")
            try:
                from app.handlers.subscription.devices import get_current_devices_detailed
                devices_info = await get_current_devices_detailed(user)
                connected_count = devices_info.get("count", 0)
                parts.append(f"Активных устройств сейчас: {connected_count}")
            except Exception:
                pass

        else:
            parts.append("\n🚫 ПОДПИСКА: Отсутствует")

        # 3. Реферальная программа
        from sqlalchemy import func
        ref_count_stmt = select(func.count(User.id)).where(User.referred_by_id == user_id)
        ref_count_res = await db.execute(ref_count_stmt)
        ref_count = ref_count_res.scalar() or 0
        
        parts.append(f"\n👥 РЕФЕРАЛЫ:")
        parts.append(f"Приглашено пользователей: {ref_count}")
        if user.referral_code:
            parts.append(f"Ваш реферальный код: {user.referral_code}")

        # 4. Ограничения
        if user.has_restrictions:
            parts.append(f"\n⚠️ ОГРАНИЧЕНИЯ АККАУНТА:")
            if user.restriction_topup: parts.append("- Запрещено пополнение баланса")
            if user.restriction_subscription: parts.append("- Запрещена покупка подписки")
            if user.restriction_reason: parts.append(f"- Причина: {user.restriction_reason}")

        # 5. Последние транзакции (3 шт)
        tx_stmt = select(Transaction).where(Transaction.user_id == user_id).order_by(Transaction.created_at.desc()).limit(3)
        tx_res = await db.execute(tx_stmt)
        txs = tx_res.scalars().all()
        if txs:
            parts.append(f"\n📑 ПОСЛЕДНИЕ ОПЕРАЦИИ:")
            for tx in txs:
                parts.append(f"- {tx.created_at.strftime('%d.%m.%Y')}: {tx.amount_rubles:.2f} руб. ({tx.type})")

        # 6. Промо-группы и уровни (Скидки)
        total_spent_kopeks = await get_user_total_spent_kopeks(db, user_id)
        parts.append(f"\n🎯 УРОВНИ И СКИДКИ:")
        parts.append(f"Всего потрачено на подписки: {total_spent_kopeks / 100:.2f} руб.")
        
        primary_group = user.get_primary_promo_group()
        if primary_group:
            parts.append(f"Текущая промо-группа: {primary_group.name}")
            discounts = []
            if primary_group.server_discount_percent: discounts.append(f"Серверы: {primary_group.server_discount_percent}%")
            if primary_group.traffic_discount_percent: discounts.append(f"Трафик: {primary_group.traffic_discount_percent}%")
            if primary_group.device_discount_percent: discounts.append(f"Устройства: {primary_group.device_discount_percent}%")
            if discounts:
                parts.append(f"Активные скидки: {', '.join(discounts)}")
        
        # Поиск следующего уровня
        promo_groups = await get_auto_assign_promo_groups(db)
        if promo_groups:
            sorted_groups = sorted(
                promo_groups,
                key=lambda g: (g.auto_assign_total_spent_kopeks or 0, g.id),
            )
            next_group = next(
                (g for g in sorted_groups if (g.auto_assign_total_spent_kopeks or 0) > total_spent_kopeks),
                None,
            )
            if next_group:
                remaining = (next_group.auto_assign_total_spent_kopeks or 0) - total_spent_kopeks
                parts.append(f"Следующий уровень: {next_group.name}")
                parts.append(f"До следующего уровня осталось потратить: {remaining / 100:.2f} руб.")
            else:
                parts.append("У вас максимальный уровень скидок.")

        return "\n".join(parts)
