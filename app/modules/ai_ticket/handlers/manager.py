"""
Manager-side handler for the DonMatteo-AI-Tiket module.

Listens for messages in the Forum group. When a manager
replies in a ticket topic, forwards the reply to the user
and auto-disables AI for that ticket.
"""

import structlog
from aiogram import Bot, Dispatcher, F, Router, types
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import User
from app.database.models_ai_ticket import ForumTicket
from app.modules.ai_ticket.services.forum_service import ForumService
from app.localization.texts import get_texts
from app.modules.ai_ticket.utils.keyboards import get_manager_kb
from app.services.system_settings_service import BotConfigurationService

logger = structlog.get_logger(__name__)

router = Router(name='ai_ticket_manager')


async def handle_manager_message(message: types.Message, bot: Bot) -> None:
    """
    A manager sent a message inside the Forum group.
    If it's in a ticket topic — forward to the user and disable AI.
    """
    forum_group_id_str = BotConfigurationService.get_current_value('SUPPORT_AI_FORUM_ID')
    if not forum_group_id_str:
        return
    
    forum_group_id = int(forum_group_id_str)

    # Only process messages in the configured forum group
    if message.chat.id != forum_group_id:
        return

    # Must be inside a topic (not the general topic)
    topic_id = message.message_thread_id
    if not topic_id:
        return

    # Ignore messages from the bot itself
    if message.from_user and message.from_user.id == bot.id:
        return

    text = message.text or message.caption or ''
    if not text.strip():
        return

    # Commands in the topic (backward compatibility or backup)
    if text.startswith('/'):
        await _handle_topic_command(message, bot, topic_id, text)
        return

    # Find the ticket for this topic with user data
    async with AsyncSessionLocal() as db:
        stmt = (
            select(ForumTicket)
            .options(joinedload(ForumTicket.user))
            .where(
                ForumTicket.telegram_topic_id == topic_id,
                ForumTicket.status == 'open'
            )
        )
        result = await db.execute(stmt)
        ticket = result.scalars().first()
        
        if not ticket or not ticket.user:
            return  # Not a ticket topic

        manager_name = message.from_user.full_name if message.from_user else 'Менеджер'

        # Forward to the user (using telegram_id!)
        try:
            await bot.send_message(
                chat_id=ticket.user.telegram_id,
                text=f'👨‍💼 <b>{manager_name}:</b>\n\n{text}',
                parse_mode='HTML',
            )
        except Exception as e:
            logger.error(
                'ai_ticket_manager.forward_to_user_failed',
                error=str(e),
                telegram_id=ticket.user.telegram_id,
                ticket_id=ticket.id
            )
            await message.reply('⚠️ Не удалось доставить сообщение пользователю.')
            return

        # Save manager message
        await ForumService.save_message(
            db=db,
            ticket_id=ticket.id,
            role='manager',
            content=text,
            message_id=message.message_id,
        )

        # Disable AI automatically if manager replied
        if ticket.ai_enabled:
            await ForumService.disable_ai(db, ticket.id)
            try:
                await bot.send_message(
                    chat_id=message.chat.id,
                    message_thread_id=topic_id,
                    text='ℹ️ AI-ассистент автоматически отключён.',
                    reply_markup=get_manager_kb(ticket.id, lang=ticket.user.language if ticket.user else 'ru', ai_enabled=False)
                )
            except Exception:
                pass
        else:
            # Just send/update controls
            try:
                await message.answer(
                    '📟 Панель управления:',
                    reply_markup=get_manager_kb(ticket.id, lang=ticket.user.language if ticket.user else 'ru', ai_enabled=False)
                )
            except Exception:
                pass

        await db.commit()


async def handle_manager_callback(callback: types.CallbackQuery, bot: Bot):
    """Handle ticket management buttons."""
    data = callback.data or ''
    async with AsyncSessionLocal() as db:
        if data.startswith('ai_ticket_close:'):
            ticket_id = int(data.split(':')[1])
            # Fetch ticket with user
            stmt = select(ForumTicket).options(joinedload(ForumTicket.user)).where(ForumTicket.id == ticket_id)
            res = await db.execute(stmt)
            ticket = res.scalars().first()
            if not ticket:
                await callback.answer('Тикет не найден')
                return
            
            await ForumService.close_ticket(db, ticket.id, bot=bot)
            await db.commit()
            
            try:
                await bot.send_message(
                    chat_id=ticket.user.telegram_id,
                    text='✅ Ваше обращение закрыто. Спасибо!',
                )
            except Exception:
                pass
            
            await callback.message.edit_text('✅ Тикет закрыт.')
            await callback.answer('Тикет закрыт')

        elif data.startswith('ai_ticket_toggle_ai:'):
            ticket_id = int(data.split(':')[1])
            stmt = select(ForumTicket).where(ForumTicket.id == ticket_id)
            res = await db.execute(stmt)
            ticket = res.scalars().first()
            if not ticket:
                await callback.answer('Ошибка')
                return
            
            new_state = not ticket.ai_enabled
            if new_state:
                await ForumService.enable_ai(db, ticket_id)
                msg = '🤖 AI-ассистент включён.'
            else:
                await ForumService.disable_ai(db, ticket_id)
                msg = '🔇 AI-ассистент выключен.'
            
            await db.commit()
            await callback.message.edit_text(msg, reply_markup=get_manager_kb(ticket_id, lang=ticket.user.language if ticket.user else 'ru', ai_enabled=new_state))
            await callback.answer(msg)


async def _handle_topic_command(
    message: types.Message,
    bot: Bot,
    topic_id: int,
    text: str,
) -> None:
    """Handle manager commands inside a ticket topic."""
    command = text.strip().lower()

    async with AsyncSessionLocal() as db:
        ticket = await ForumService.get_ticket_by_topic_id(db, topic_id)
        if not ticket:
            return

        if command == '/close':
            await ForumService.close_ticket(db, ticket.id)
            await db.commit()
            # We skip user notification here to avoid double notify if used with buttons
            await message.reply('✅ Тикет закрыт.')

        elif command == '/ai_on':
            await ForumService.enable_ai(db, ticket.id)
            await db.commit()
            await message.reply('🤖 AI-ассистент включён.', reply_markup=get_manager_kb(ticket.id, lang=ticket.user.language if ticket.user else 'ru', ai_enabled=True))

        elif command == '/ai_off':
            await ForumService.disable_ai(db, ticket.id)
            await db.commit()
            await message.reply('🔇 AI-ассистент выключен.', reply_markup=get_manager_kb(ticket.id, lang=ticket.user.language if ticket.user else 'ru', ai_enabled=False))


def register_manager_handlers(dp: Dispatcher) -> None:
    """Register manager handlers."""
    forum_group_id_str = BotConfigurationService.get_current_value('SUPPORT_AI_FORUM_ID')
    if not forum_group_id_str:
        return

    f_id = int(forum_group_id_str)
    
    dp.message.register(
        handle_manager_message,
        F.chat.id == f_id,
    )
    
    dp.callback_query.register(
        handle_manager_callback,
        F.data.startswith('ai_ticket_close:') | F.data.startswith('ai_ticket_toggle_ai:'),
    )
