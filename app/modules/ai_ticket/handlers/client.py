"""
Client-side handler for the DonMatteo-AI-Tiket module.

Intercepts user messages when SUPPORT_SYSTEM_MODE == 'ai_tiket',
creates Forum topics, calls AI, and routes replies.
"""

import structlog
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import StateFilter
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.database.models import User
from app.modules.ai_ticket.services import ai_manager
from app.modules.ai_ticket.services.forum_service import ForumService
from app.modules.ai_ticket.services import prompt_service
from app.localization.texts import get_texts
from app.modules.ai_ticket.utils.keyboards import get_manager_kb, get_user_navigation_kb
from app.services.system_settings_service import BotConfigurationService

logger = structlog.get_logger(__name__)

router = Router(name='ai_ticket_client')


async def handle_ai_ticket_message(
    message: types.Message,
    bot: Bot,
    db: AsyncSession,
    db_user: User,
) -> None:
    """Main entry point for AI support logic (callback from _ai_ticket_message_proxy)."""
    logger.info('ai_ticket_client.handle_message_started', chat_id=message.chat.id, user_id=db_user.id)
    user_text = message.text or message.caption or ''
    if not user_text.strip():
        return

    # 1. Get or create ticket
    texts = get_texts(db_user.language)
    try:
        ticket = await ForumService.get_or_create_ticket(db, bot, db_user.id, db_user.full_name)
    except Exception as e:
        logger.error('ai_ticket_client.ticket_init_failed', error=str(e), user_id=db_user.id)
        await message.answer(texts.t('TICKET_CREATE_ERROR', '⚠️ Ошибка инициализации тикета. Мы скоро свяжемся с вами.'))
        return

    # 2. Get Forum Group ID
    forum_group_id_str = BotConfigurationService.get_current_value('SUPPORT_AI_FORUM_ID')
    if not forum_group_id_str:
        logger.error('ai_ticket_client.no_forum_id')
        return
    forum_group_id = int(forum_group_id_str)

    # 3. Check AI state
    ai_enabled_global = BotConfigurationService.get_current_value('SUPPORT_AI_ENABLED')
    if isinstance(ai_enabled_global, str):
        ai_enabled_global = ai_enabled_global.lower() in ('true', '1', 'on', 'yes')
    
    should_run_ai = ticket.ai_enabled and ai_enabled_global

    # 4. Save user message in DB
    await ForumService.save_message(db=db, ticket_id=ticket.id, role='user', content=user_text)

    # 5. Forward user message to forum topic with buttons
    try:
        await bot.send_message(
            chat_id=forum_group_id,
            message_thread_id=ticket.telegram_topic_id,
            text=f"👤 <b>Сообщение от пользователя {db_user.full_name}:</b>\n\n{user_text}",
            parse_mode='HTML',
            reply_markup=get_manager_kb(ticket.id, lang='ru', ai_enabled=ticket.ai_enabled)
        )
    except Exception as e:
        logger.error('ai_ticket_client.forward_to_manager_failed', error=str(e))

    # 6. Immediate feedback to user
    status_text = texts.t('AI_TICKET_MESSAGE_RECEIVED', '⏳ <b>Ваше сообщение получено.</b>')
    if should_run_ai:
        status_text += "\n<i>ИИ-ассистент обдумывает ответ...</i>"
    else:
        status_text += "\n<i>Менеджеры уведомлены и скоро ответят.</i>"

    status_msg = await message.answer(
        status_text,
        parse_mode='HTML',
        reply_markup=get_user_navigation_kb(ticket.id, lang=db_user.language, show_call_manager=ticket.ai_enabled)
    )

    if not should_run_ai:
        await db.commit()
        return

    # 7. AI response processing with failover
    try:
        await ai_manager.ensure_providers_exist(db)
        system_prompt = await prompt_service.get_system_prompt(db)

        # FAQ & User Context
        faq_articles = await ForumService.get_active_faq_articles(db)
        faq_context = ForumService.format_faq_context(faq_articles)
        if faq_context:
            system_prompt += f'\n\n## БАЗА ЗНАНИЙ:\n{faq_context}'

        user_context_parts: list[str] = []
        if hasattr(db_user, 'balance'):
            user_context_parts.append(f'Баланс: {(db_user.balance or 0) / 100:.2f} руб.')
        if user_context_parts:
            system_prompt += '\n\n## КОНТЕКСТ ПОЛЬЗОВАТЕЛЯ:\n' + '\n'.join(user_context_parts)

        # History and Generation
        history = await ForumService.get_conversation_history(db, ticket.id)
        messages_ai = [{'role': 'system', 'content': system_prompt}] + history

        ai_response = await ai_manager.generate_ai_response(db=db, messages=messages_ai)

        if ai_response:
            # Save and send to user
            await ForumService.save_message(db=db, ticket_id=ticket.id, role='ai', content=ai_response)
            
            await status_msg.edit_text(
                f'🤖 <b>AI-ассистент:</b>\n\n{ai_response}',
                parse_mode='HTML',
                reply_markup=get_user_navigation_kb(ticket.id, lang=db_user.language, show_call_manager=ticket.ai_enabled)
            )

            # Duplicate in Forum
            try:
                await bot.send_message(
                    chat_id=forum_group_id,
                    message_thread_id=ticket.telegram_topic_id,
                    text=f'🤖 <b>AI-Ответ</b>:\n\n{ai_response}',
                    parse_mode='HTML',
                )
            except Exception as e:
                logger.error('ai_ticket_client.forum_copy_failed', error=str(e))
        else:
            # AI failed or returned None
            await status_msg.edit_text(
                texts.t('AI_TICKET_UNAVAILABLE', "🤖 <b>AI-ассистент временно недоступен.</b>\n\nВаше сообщение передано менеджерам. Ожидайте ответа специалиста."),
                parse_mode='HTML',
                reply_markup=get_user_navigation_kb(ticket.id, lang=db_user.language, show_call_manager=ticket.ai_enabled)
            )
            
    except Exception as e:
        logger.error('ai_ticket_client.ai_processing_failed', error=str(e))
        try:
            await status_msg.edit_text(
                texts.t('AI_TICKET_ERROR', "⚠️ <b>Сообщение доставлено поддержке.</b>\n\nМы ответим вам в ближайшее время."),
                parse_mode='HTML',
                reply_markup=get_user_navigation_kb(ticket.id, lang=db_user.language, show_call_manager=ticket.ai_enabled)
            )
        except Exception:
            pass

    await db.commit()


async def handle_call_manager(
    callback: types.CallbackQuery,
    bot: Bot,
    db: AsyncSession,
    db_user: User,
) -> None:
    """User pressed 'Позвать менеджера' — disable AI and notify."""
    data = callback.data or ''
    parts = data.split(':')
    if len(parts) != 2:
        await callback.answer('Ошибка', show_alert=True)
        return

    try:
        ticket_id = int(parts[1])
    except ValueError:
        await callback.answer('Ошибка', show_alert=True)
        return

    await ForumService.disable_ai(db, ticket_id)
    await db.commit()

    # REMOVE BUTTON FROM SOURCE MESSAGE TO PREVENT SPAM
    try:
        await callback.message.edit_reply_markup(
            reply_markup=get_user_navigation_kb(ticket_id, lang=db_user.language, show_call_manager=False)
        )
    except Exception:
        pass

    # Notify in Forum topic
    forum_group_id_str = BotConfigurationService.get_current_value('SUPPORT_AI_FORUM_ID')
    if forum_group_id_str:
        from app.database.models_ai_ticket import ForumTicket
        stmt = select(ForumTicket).where(ForumTicket.id == ticket_id)
        result = await db.execute(stmt)
        ticket = result.scalars().first()
        if ticket and ticket.telegram_topic_id:
            try:
                await bot.send_message(
                    chat_id=int(forum_group_id_str),
                    message_thread_id=ticket.telegram_topic_id,
                    text='⚠️ <b>Клиент вызвал менеджера.</b> AI-ассистент отключён.',
                    parse_mode='HTML',
                    reply_markup=get_manager_kb(ticket_id, lang='ru', ai_enabled=False)
                )
            except Exception as e:
                logger.error('ai_ticket_client.manager_notify_failed', error=str(e))

    texts = get_texts(db_user.language)
    await callback.message.answer(
        texts.t('AI_TICKET_MANAGER_CALLED', '👨‍💻 Менеджер подключится к вашему обращению в ближайшее время. AI-ассистент отключён.'),
        reply_markup=get_user_navigation_kb(ticket_id, lang=db_user.language, show_call_manager=False)
    )
    await callback.answer()


def register_client_handlers(dp: Dispatcher) -> None:
    """Register the 'Call manager' callback."""
    dp.callback_query.register(
        handle_call_manager,
        F.data.startswith('ai_ticket_call_manager:'),
    )
