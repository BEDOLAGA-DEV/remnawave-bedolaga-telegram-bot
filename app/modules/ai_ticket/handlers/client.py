"""
Client-side handler для модуля DonMatteo-AI-Tiket.

Перехватывает сообщения пользователя при SUPPORT_SYSTEM_MODE == 'ai_tiket',
создаёт Forum-топики, вызывает AI, маршрутизирует ответы.
Поддержка фото-вложений.
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
from app.modules.ai_ticket.utils.keyboards import get_manager_kb, get_user_navigation_kb, get_user_reply_kb
from app.modules.ai_ticket.utils.formatting import sanitize_ai_response
from app.modules.ai_ticket.utils.media_sender import extract_and_send_media
from app.services.system_settings_service import BotConfigurationService

logger = structlog.get_logger(__name__)

router = Router(name='ai_ticket_client')


async def handle_ai_ticket_message(
    message: types.Message,
    bot: Bot,
    db: AsyncSession,
    db_user: User,
) -> None:
    """Основная точка входа для AI-поддержки (callback из _ai_ticket_message_proxy). Поддерживает фото."""
    logger.info('ai_ticket_client.handle_message_started', chat_id=message.chat.id, user_id=db_user.id)
    user_text = message.text or message.caption or ''

    # Извлекаем медиа
    media_type = None
    media_file_id = None
    if message.photo:
        media_type = 'photo'
        media_file_id = message.photo[-1].file_id

    # Пустое сообщение без медиа — не обрабатываем
    if not user_text.strip() and not media_file_id:
        return

    # Защита от спама (одинаковое сообщение дважды подряд)
    from app.database.models_ai_ticket import ForumTicketMessage, ForumTicket
    stmt_spam = select(ForumTicketMessage).join(ForumTicket).where(
        ForumTicket.user_id == db_user.id,
        ForumTicketMessage.role == 'user'
    ).order_by(ForumTicketMessage.created_at.desc()).limit(1)
    last_msg_res = await db.execute(stmt_spam)
    last_msg = last_msg_res.scalars().first()
    
    if last_msg and last_msg.content == user_text and getattr(last_msg, 'media_file_id', None) == media_file_id:
        from datetime import datetime, timezone
        if last_msg.created_at:
            delta = (datetime.now(timezone.utc) - last_msg.created_at).total_seconds()
            if delta < 300:  # Если отправлено то же самое в течение 5 минут - игнорируем
                return

    # 1. Создаём/получаем тикет
    texts = get_texts(db_user.language)
    try:
        ticket = await ForumService.get_or_create_ticket(db, bot, db_user.id, db_user.full_name)
        if not ticket:
            logger.error('ai_ticket_client.ticket_init_returned_none', user_id=db_user.id)
            await message.answer(texts.t('TICKET_CREATE_ERROR', '⚠️ Не удалось создать обращение. Пожалуйста, попробуйте позже или используйте другой способ связи.'))
            return
    except Exception as e:
        logger.error('ai_ticket_client.ticket_init_failed', error=str(e), user_id=db_user.id)
        await message.answer(texts.t('TICKET_CREATE_ERROR', '⚠️ Ошибка инициализации тикета. Мы скоро свяжемся с вами.'))
        return

    # 2. ID форум-группы
    forum_group_id_val = BotConfigurationService.get_current_value('SUPPORT_AI_FORUM_ID')
    try:
        forum_group_id = int(forum_group_id_val) if forum_group_id_val else None
    except (ValueError, TypeError):
        logger.error('ai_ticket_client.invalid_forum_id', value=forum_group_id_val)
        forum_group_id = None
        
    if not forum_group_id:
        logger.warning('ai_ticket_client.no_forum_id — форум-группа не настроена, пересылка менеджерам отключена')

    # 3. Проверяем состояние AI
    ai_enabled_global = BotConfigurationService.get_current_value('SUPPORT_AI_ENABLED')
    if isinstance(ai_enabled_global, str):
        ai_enabled_global = ai_enabled_global.lower() in ('true', '1', 'on', 'yes')

    should_run_ai = bool(ticket.ai_enabled and ai_enabled_global)
    
    logger.info('ai_ticket_client.ai_status', 
                ticket_id=ticket.id, 
                ticket_ai=ticket.ai_enabled, 
                global_ai=ai_enabled_global, 
                should_run=should_run_ai)

    # 4. Сохраняем сообщение пользователя с медиа
    await ForumService.save_message(
        db=db,
        ticket_id=ticket.id,
        role='user',
        content=user_text or '[фото]',
        media_type=media_type,
        media_file_id=media_file_id,
    )

    # 5. Пересылаем сообщение в форум-топик менеджеру
    if forum_group_id and ticket.telegram_topic_id:
        try:
            if media_type == 'photo' and media_file_id:
                caption_text = f"👤 <b>Сообщение от пользователя {db_user.full_name}:</b>\n\n{user_text}" if user_text else f"👤 <b>Фото от пользователя {db_user.full_name}</b>"
                await bot.send_photo(
                    chat_id=forum_group_id,
                    message_thread_id=ticket.telegram_topic_id,
                    photo=media_file_id,
                    caption=caption_text,
                    parse_mode='HTML',
                    reply_markup=get_manager_kb(ticket.id, ai_enabled=ticket.ai_enabled),
                )
            else:
                await bot.send_message(
                    chat_id=forum_group_id,
                    message_thread_id=ticket.telegram_topic_id,
                    text=f"👤 <b>Сообщение от пользователя {db_user.full_name}:</b>\n\n{user_text}",
                    parse_mode='HTML',
                    reply_markup=get_manager_kb(ticket.id, ai_enabled=ticket.ai_enabled),
                )
        except Exception as e:
            logger.error('ai_ticket_client.forward_to_manager_failed', error=str(e))

    # 6. Мгновенная обратная связь пользователю
    status_text = texts.t('AI_TICKET_MESSAGE_RECEIVED', '⏳ <b>Ваше сообщение получено.</b>')
    if should_run_ai:
        status_text += f"\n\n<i>ИИ-ассистент обдумывает ответ...</i>"
    else:
        status_text += f"\n\n<i>Менеджеры уведомлены и скоро ответят.</i>"

    status_msg = await message.answer(
        status_text,
        parse_mode='HTML',
        reply_markup=get_user_reply_kb(ticket.id, lang=db_user.language, show_call_manager=False)
    )

    if not should_run_ai:
        # AI отключён — уведомляем менеджера в топике
        if forum_group_id and ticket.telegram_topic_id:
            try:
                await bot.send_message(
                    chat_id=forum_group_id,
                    message_thread_id=ticket.telegram_topic_id,
                    text='⚠️ <b>AI-ассистент отключён.</b>\nПожалуйста, ответьте пользователю вручную или включите AI.',
                    parse_mode='HTML',
                    reply_markup=get_manager_kb(ticket.id, lang='ru', ai_enabled=False),
                )
            except Exception as e:
                logger.error('ai_ticket_client.manager_ai_disabled_notify_failed', error=str(e))
        await db.commit()
        return

    # 7. AI обработка с фоллбеком
    try:
        await ai_manager.ensure_providers_exist(db)
        system_prompt = await prompt_service.get_system_prompt(db)

        # FAQ и контекст пользователя
        faq_articles = await ForumService.get_active_faq_articles(db)
        faq_context = ForumService.format_faq_context(faq_articles)
        if faq_context:
            system_prompt += f'\n\n## БАЗА ЗНАНИЙ:\n{faq_context}'

        user_context = await ForumService.get_rich_user_context(db, db_user.id)
        if user_context:
            system_prompt += f'\n\n## КОНТЕКСТ ПОЛЬЗОВАТЕЛЯ:\n{user_context}'

        # История и генерация
        history = await ForumService.get_conversation_history(db, ticket.id)
        messages_ai = [{'role': 'system', 'content': system_prompt}] + history

        ai_response = await ai_manager.generate_ai_response(db=db, messages=messages_ai)

        if ai_response:
            logger.info('ai_ticket_client.raw_response', response=ai_response)
            
            # Чистим think-блоки ДО проверки тегов
            import re as _re
            cleaned_response = _re.sub(r'<think>.*?</think>', '', ai_response, flags=_re.DOTALL).strip()
            logger.info('ai_ticket_client.cleaned_response', response=cleaned_response)

            # Проверяем триггеры автовызова менеджера (регуляркой для надежности)
            cleaned_lower = cleaned_response.lower()
            
            spam_trigger_pattern = _re.compile(r'\[\s*spam_call_manager\s*\]', _re.IGNORECASE)
            general_trigger_pattern = _re.compile(
                r'\[\s*(call_manager|call manager|call-manager|manager|вызов_менеджера|позвать_менеджера|помощь|help)\s*\]|'
                r'(call_manager|call manager|call-manager|вызов_менеджера|позвать_менеджера)', 
                _re.IGNORECASE
            )
            
            is_spam = bool(spam_trigger_pattern.search(cleaned_lower))
            is_general = bool(general_trigger_pattern.search(cleaned_lower))
            
            if is_spam or is_general:
                # Отключаем ИИ
                ticket.ai_enabled = False
                await db.commit()
                
                # Сообщаем менеджеру в топике
                if forum_group_id and ticket.telegram_topic_id:
                    manager_msg = (
                        '⚠️ <b>АВТОВЫЗОВ (СПАМ):</b> Пользователь настойчиво повторяет вопрос. ИИ отключён.'
                        if is_spam else
                        '⚠️ <b>АВТОВЫЗОВ:</b> ИИ не смог найти ответ в FAQ и перевел тикет на менеджера. AI-ассистент отключён.'
                    )
                    try:
                        await bot.send_message(
                            chat_id=forum_group_id,
                            message_thread_id=ticket.telegram_topic_id,
                            text=manager_msg,
                            parse_mode='HTML',
                            reply_markup=get_manager_kb(ticket.id, lang='ru', ai_enabled=False)
                        )
                    except Exception as e:
                        logger.error('ai_ticket_client.manager_notify_failed', error=str(e))
                
                # Сообщаем пользователю соответствующим текстом
                if is_spam:
                    msg_text = texts.t('AI_TICKET_SPAM_CALLED', '🤖 <b>AI-ассистент:</b>\nПохоже, мой ответ не помог, и вы продолжаете задавать один и тот же вопрос. Я передал ваше обращение менеджеру для уточнения деталей. Пожалуйста, ожидайте.')
                else:
                    msg_text = texts.t('AI_TICKET_MANAGER_AUTO_CALLED', '🤖 <b>AI-ассистент:</b>\nК сожалению, я не знаю точного ответа на ваш вопрос. Я передал ваше обращение менеджеру, пожалуйста, ожидайте ответа специалиста.')
                
                await status_msg.edit_text(
                    msg_text,
                    reply_markup=get_user_navigation_kb(ticket.id, lang=db_user.language, show_call_manager=False),
                    parse_mode='HTML'
                )
                await db.commit()
                return

            # Принудительно вырезаем любые упоминания триггеров из финального текста
            cleaned_response = spam_trigger_pattern.sub('', cleaned_response)
            cleaned_response = general_trigger_pattern.sub('', cleaned_response).strip()
            
            # Формальный ответ от ИИ (только если не было CALL_MANAGER)
            await ForumService.save_message(db=db, ticket_id=ticket.id, role='ai', content=cleaned_response)
            safe_response = sanitize_ai_response(cleaned_response)

            # Извлекаем медиа-теги и отправляем медиа пользователю
            safe_response = await extract_and_send_media(
                bot=bot,
                chat_id=message.chat.id,
                ai_response=safe_response,
                db=db,
            )

            await status_msg.edit_text(
                f'🤖 <b>AI-ассистент:</b>\n\n{safe_response}',
                parse_mode='HTML',
                reply_markup=get_user_reply_kb(ticket.id, lang=db_user.language, show_call_manager=ticket.ai_enabled)
            )

            # Дублируем текст в форум
            try:
                await bot.send_message(
                    chat_id=forum_group_id,
                    message_thread_id=ticket.telegram_topic_id,
                    text=f'🤖 <b>AI-Ответ</b>:\n\n{safe_response}',
                    parse_mode='HTML',
                )
            except Exception as e:
                logger.error('ai_ticket_client.forum_copy_failed', error=str(e))

            # Дублируем медиа в форум-топик (чтобы менеджер видел)
            if forum_group_id and ticket.telegram_topic_id:
                from app.modules.ai_ticket.utils.media_sender import extract_media_tags, get_media_by_tags
                media_tags = extract_media_tags(sanitize_ai_response(cleaned_response))
                if media_tags:
                    media_items = await get_media_by_tags(db, media_tags)
                    for m in media_items:
                        try:
                            if m.media_type == 'photo':
                                await bot.send_photo(
                                    chat_id=forum_group_id,
                                    message_thread_id=ticket.telegram_topic_id,
                                    photo=m.file_id,
                                    caption=f'📎 Медиа отправлено пользователю: {m.tag}',
                                )
                            elif m.media_type == 'video':
                                await bot.send_video(
                                    chat_id=forum_group_id,
                                    message_thread_id=ticket.telegram_topic_id,
                                    video=m.file_id,
                                    caption=f'📎 Медиа отправлено пользователю: {m.tag}',
                                )
                            elif m.media_type == 'animation':
                                await bot.send_animation(
                                    chat_id=forum_group_id,
                                    message_thread_id=ticket.telegram_topic_id,
                                    animation=m.file_id,
                                    caption=f'📎 Медиа отправлено пользователю: {m.tag}',
                                )
                        except Exception as e:
                            logger.error('ai_ticket_client.forum_media_copy_failed', tag=m.tag, error=str(e))
        else:
            # AI не сработал
            await status_msg.edit_text(
                texts.t('AI_TICKET_UNAVAILABLE', "🤖 <b>AI-ассистент временно недоступен.</b>\n\nВаше сообщение передано менеджерам. Ожидайте ответа специалиста."),
                parse_mode='HTML',
                reply_markup=get_user_reply_kb(ticket.id, lang=db_user.language, show_call_manager=ticket.ai_enabled)
            )

    except Exception as e:
        logger.error('ai_ticket_client.ai_processing_failed', error=str(e))
        try:
            await status_msg.edit_text(
                texts.t('AI_TICKET_ERROR', "⚠️ <b>Сообщение доставлено поддержке.</b>\n\nМы ответим вам в ближайшее время."),
                parse_mode='HTML',
                reply_markup=get_user_reply_kb(ticket.id, lang=db_user.language, show_call_manager=ticket.ai_enabled)
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
    """Пользователь нажал 'Позвать менеджера' — отключаем AI и уведомляем."""
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

    # Убираем кнопку вызова менеджера
    try:
        await callback.message.edit_reply_markup(
            reply_markup=get_user_navigation_kb(ticket_id, lang=db_user.language, show_call_manager=False)
        )
    except Exception:
        pass

    # Уведомляем в форум-топик
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


async def handle_close_media(callback: types.CallbackQuery) -> None:
    """Пользователь нажал '❌ Закрыть' под медиа сообщением ИИ."""
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


def register_client_handlers(dp: Dispatcher) -> None:
    """Регистрация callback 'Вызвать менеджера' и закрытия медиа."""
    dp.callback_query.register(
        handle_call_manager,
        F.data.startswith('ai_ticket_call_manager:'),
    )
    dp.callback_query.register(
        handle_close_media,
        F.data == 'ai_faq_media_close',
    )
