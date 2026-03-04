"""
Manager-side handler для модуля DonMatteo-AI-Tiket.

Слушает сообщения в Forum-группе. Когда менеджер
отвечает в топике тикета — пересылает ответ пользователю
(с кнопками навигации как в оригинале) и автоотключает AI.
Поддержка медиа (фото).
Кнопки управления НЕ дублируются на каждом сообщении.
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
    Менеджер отправил сообщение в Forum-группе.
    Если это топик тикета — пересылаем пользователю с кнопками навигации.
    Поддерживает фото-вложения.
    """
    forum_group_id_str = BotConfigurationService.get_current_value('SUPPORT_AI_FORUM_ID')
    if not forum_group_id_str:
        return

    forum_group_id = int(forum_group_id_str)

    # Обрабатываем только сообщения в настроенной форум-группе
    if message.chat.id != forum_group_id:
        return

    # Должно быть внутри топика (не General)
    topic_id = message.message_thread_id
    if not topic_id:
        return

    # Игнорируем сообщения от самого бота
    if message.from_user and message.from_user.id == bot.id:
        return

    # Извлекаем текст и медиа
    text = message.text or message.caption or ''
    media_type = None
    media_file_id = None

    if message.photo:
        media_type = 'photo'
        media_file_id = message.photo[-1].file_id

    # Команды в топике
    if text.startswith('/'):
        await _handle_topic_command(message, bot, topic_id, text)
        return

    # Пустое сообщение без медиа — не обрабатываем
    if not text.strip() and not media_file_id:
        return

    # Ищем тикет по topic_id с данными пользователя
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
            return  # Не тикетный топик

        manager_name = message.from_user.full_name if message.from_user else 'Менеджер'
        user_chat_id = ticket.user.telegram_id
        user_lang = ticket.user.language if ticket.user else 'ru'
        texts = get_texts(user_lang)

        # Формируем уведомление как в оригинале
        reply_preview = text[:100] + '...' if len(text) > 100 else text
        notification_text = texts.t(
            'TICKET_REPLY_NOTIFICATION',
            '🎫 Получен ответ по тикету #{ticket_id}\n\n{reply_preview}\n\nНажмите кнопку ниже, чтобы перейти к тикету:',
        ).format(ticket_id=ticket.id, reply_preview=reply_preview)

        # Клавиатура уведомления (как в оригинале)
        notification_kb = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('VIEW_TICKET', '👁️ Посмотреть тикет'),
                        callback_data=f'view_forum_ticket_{ticket.id}'
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('REPLY_TO_TICKET', '💬 Ответить'),
                        callback_data=f'reply_forum_ticket_{ticket.id}'
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CLOSE_NOTIFICATION', '❌ Закрыть уведомление'),
                        callback_data=f'close_ticket_notification_{ticket.id}'
                    )
                ],
            ]
        )

        # Отправляем уведомление пользователю
        try:
            if media_type == 'photo' and media_file_id:
                # Фото-уведомление
                await bot.send_photo(
                    chat_id=user_chat_id,
                    photo=media_file_id,
                    caption=notification_text,
                    reply_markup=notification_kb,
                    parse_mode='HTML',
                )
            else:
                # Текстовое уведомление
                await bot.send_message(
                    chat_id=user_chat_id,
                    text=notification_text,
                    reply_markup=notification_kb,
                    parse_mode='HTML',
                )
        except Exception as e:
            logger.error(
                'ai_ticket_manager.forward_to_user_failed',
                error=str(e),
                telegram_id=user_chat_id,
                ticket_id=ticket.id
            )
            await message.reply('⚠️ Не удалось доставить сообщение пользователю.')
            return

        # Сохраняем сообщение менеджера с медиа
        await ForumService.save_message(
            db=db,
            ticket_id=ticket.id,
            role='manager',
            content=text or '[фото]',
            message_id=message.message_id,
            media_type=media_type,
            media_file_id=media_file_id,
        )

        # Автоматически отключаем AI если менеджер ответил (только уведомление, БЕЗ кнопок)
        if ticket.ai_enabled:
            await ForumService.disable_ai(db, ticket.id)
            try:
                await bot.send_message(
                    chat_id=message.chat.id,
                    message_thread_id=topic_id,
                    text='ℹ️ AI-ассистент автоматически отключён.',
                    reply_markup=get_manager_kb(ticket.id, lang=user_lang, ai_enabled=False)
                )
            except Exception:
                pass
        # НЕ отправляем кнопки управления на каждое сообщение менеджера —
        # кнопки показываются только при системных событиях (выкл AI, вызов менеджера и т.д.)

        await db.commit()


async def handle_manager_callback(callback: types.CallbackQuery, bot: Bot):
    """Обработка кнопок управления тикетом менеджером."""
    data = callback.data or ''
    async with AsyncSessionLocal() as db:
        if data.startswith('ai_ticket_close:'):
            ticket_id = int(data.split(':')[1])
            # Загружаем тикет с пользователем
            stmt = select(ForumTicket).options(joinedload(ForumTicket.user)).where(ForumTicket.id == ticket_id)
            res = await db.execute(stmt)
            ticket = res.scalars().first()
            if not ticket:
                await callback.answer('Тикет не найден')
                return

            await ForumService.close_ticket(db, ticket.id, bot=bot)
            await db.commit()

            try:
                from app.localization.texts import get_texts
                user_lang = ticket.user.language if ticket.user else 'ru'
                texts_user = get_texts(user_lang)
                
                close_kb = types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(text=texts_user.t('CLOSE_MESSAGE_BTN', '❌ Закрыть'), callback_data='ai_faq_media_close'),
                            types.InlineKeyboardButton(text=texts_user.t('MAIN_MENU_BUTTON', '🏠 На главную'), callback_data='back_to_menu')
                        ]
                    ]
                )
                
                await bot.send_message(
                    chat_id=ticket.user.telegram_id,
                    text='✅ Ваше обращение закрыто. Спасибо!',
                    reply_markup=close_kb
                )
            except Exception:
                pass

            await callback.message.edit_text('✅ Тикет закрыт.')
            await callback.answer('Тикет закрыт')

        elif data.startswith('ai_ticket_toggle_ai:'):
            ticket_id = int(data.split(':')[1])
            stmt = select(ForumTicket).options(joinedload(ForumTicket.user)).where(ForumTicket.id == ticket_id)
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
            
            # Для избежания MissingGreenlet при обращении к ticket.user
            lang = 'ru'
            if ticket and ticket.user:
                lang = ticket.user.language

            await callback.message.edit_text(msg, reply_markup=get_manager_kb(ticket_id, lang=lang, ai_enabled=new_state))
            await callback.answer(msg)


async def _handle_topic_command(
    message: types.Message,
    bot: Bot,
    topic_id: int,
    text: str,
) -> None:
    """Обработка команд менеджера внутри топика тикета."""
    command = text.strip().lower()

    async with AsyncSessionLocal() as db:
        ticket = await ForumService.get_ticket_by_topic_id(db, topic_id)
        if not ticket:
            return

        if command == '/close':
            await ForumService.close_ticket(db, ticket.id)
            await db.commit()
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
    """Регистрация обработчиков менеджера."""
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
