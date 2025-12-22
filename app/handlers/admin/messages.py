import html
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from aiogram import Dispatcher, types, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import InterfaceError
from sqlalchemy import select, func, and_, or_

from app.config import settings
from app.states import AdminStates
from app.database.models import (
    User,
    UserStatus,
    Subscription,
    SubscriptionStatus,
    BroadcastHistory,
)
from app.database.database import AsyncSessionLocal
from app.keyboards.admin import (
    get_admin_messages_keyboard, get_broadcast_target_keyboard,
    get_custom_criteria_keyboard, get_broadcast_history_keyboard,
    get_admin_pagination_keyboard, get_broadcast_media_keyboard,
    get_media_confirm_keyboard, get_updated_message_buttons_selector_keyboard_with_media,
    BROADCAST_BUTTON_ROWS, DEFAULT_BROADCAST_BUTTONS,
    get_broadcast_button_config, get_broadcast_button_labels, get_pinned_message_keyboard
)
from app.localization.texts import get_texts
from app.database.crud.user import get_users_list
from app.database.crud.subscription import get_expiring_subscriptions
from app.utils.decorators import admin_required, error_handler
from app.utils.miniapp_buttons import build_miniapp_or_callback_button
from app.services.pinned_message_service import (
    broadcast_pinned_message,
    get_active_pinned_message,
    set_active_pinned_message,
)

logger = logging.getLogger(__name__)

BUTTON_ROWS = BROADCAST_BUTTON_ROWS
DEFAULT_SELECTED_BUTTONS = DEFAULT_BROADCAST_BUTTONS

TEXT_MENU_MINIAPP_BUTTON_KEYS = {
    "balance",
    "referrals",
    "promocode",
    "connect",
    "subscription",
}


def get_message_buttons_selector_keyboard(language: str = "ru") -> types.InlineKeyboardMarkup:
    return get_updated_message_buttons_selector_keyboard(list(DEFAULT_SELECTED_BUTTONS), language)


def get_updated_message_buttons_selector_keyboard(selected_buttons: list, language: str = "ru") -> types.InlineKeyboardMarkup:
    return get_updated_message_buttons_selector_keyboard_with_media(selected_buttons, False, language)


def create_broadcast_keyboard(selected_buttons: list, language: str = "ru") -> Optional[types.InlineKeyboardMarkup]:
    selected_buttons = selected_buttons or []
    keyboard: list[list[types.InlineKeyboardButton]] = []
    button_config_map = get_broadcast_button_config(language)

    for row in BUTTON_ROWS:
        row_buttons: list[types.InlineKeyboardButton] = []
        for button_key in row:
            if button_key not in selected_buttons:
                continue
            button_config = button_config_map[button_key]
            if settings.is_text_main_menu_mode() and button_key in TEXT_MENU_MINIAPP_BUTTON_KEYS:
                row_buttons.append(
                    build_miniapp_or_callback_button(
                        text=button_config["text"],
                        callback_data=button_config["callback"],
                    )
                )
            else:
                row_buttons.append(
                    types.InlineKeyboardButton(
                        text=button_config["text"],
                        callback_data=button_config["callback"]
                    )
                )
        if row_buttons:
            keyboard.append(row_buttons)

    if not keyboard:
        return None

    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


async def _persist_broadcast_result(
    db: AsyncSession,
    broadcast_history: BroadcastHistory,
    sent_count: int,
    failed_count: int,
    status: str,
) -> None:
    """Сохраняет результаты рассылки с повторной попыткой при обрыве соединения."""

    broadcast_history.sent_count = sent_count
    broadcast_history.failed_count = failed_count
    broadcast_history.status = status
    broadcast_history.completed_at = datetime.utcnow()

    try:
        await db.commit()
        return
    except InterfaceError as error:
        logger.warning(
            "Соединение с БД потеряно при сохранении результатов рассылки, пробуем еще раз",
            exc_info=error,
        )
        await db.rollback()

    try:
        async with AsyncSessionLocal() as retry_session:
            retry_history = await retry_session.get(BroadcastHistory, broadcast_history.id)
            if not retry_history:
                logger.critical(
                    "Не удалось найти запись BroadcastHistory #%s для повторной записи результатов",
                    broadcast_history.id,
                )
                return

            retry_history.sent_count = sent_count
            retry_history.failed_count = failed_count
            retry_history.status = status
            retry_history.completed_at = broadcast_history.completed_at
            await retry_session.commit()
            logger.info(
                "Результаты рассылки успешно сохранены после повторного подключения к БД (id=%s)",
                broadcast_history.id,
            )
    except Exception as retry_error:
        logger.critical(
            "Не удалось сохранить результаты рассылки после восстановления подключения",
            exc_info=retry_error,
        )


@admin_required
@error_handler
async def show_messages_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    text = """
📨 <b>Управление рассылками</b>

Выберите тип рассылки:

- <b>Всем пользователям</b> - рассылка всем активным пользователям
- <b>По подпискам</b> - фильтрация по типу подписки
- <b>По критериям</b> - настраиваемые фильтры
- <b>История</b> - просмотр предыдущих рассылок

⚠️ Будьте осторожны с массовыми рассылками!
"""
    
    await callback.message.edit_text(
        text,
        reply_markup=get_admin_messages_keyboard(db_user.language),
        parse_mode="HTML"  
    )
    await callback.answer()


@admin_required
@error_handler
async def show_pinned_message_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    await state.clear()
    pinned_message = await get_active_pinned_message(db)

    if pinned_message:
        content_preview = html.escape(pinned_message.content or "")
        last_updated = pinned_message.updated_at or pinned_message.created_at
        timestamp_text = last_updated.strftime("%d.%m.%Y %H:%M") if last_updated else "—"
        media_line = ""
        if pinned_message.media_type:
            media_label = "Фото" if pinned_message.media_type == "photo" else "Видео"
            media_line = f"📎 Медиа: {media_label}\n"
        body = (
            "📌 <b>Закрепленное сообщение</b>\n\n"
            "📝 Текущий текст:\n"
            f"<code>{content_preview}</code>\n\n"
            f"{media_line}"
            f"🕒 Обновлено: {timestamp_text}"
        )
    else:
        body = (
            "📌 <b>Закрепленное сообщение</b>\n\n"
            "Сообщение не задано. Отправьте новый текст, чтобы разослать и закрепить его у пользователей."
        )

    await callback.message.edit_text(
        body,
        reply_markup=get_pinned_message_keyboard(db_user.language),
        parse_mode="HTML",
    )
    await callback.answer()


@admin_required
@error_handler
async def prompt_pinned_message_update(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
):
    await state.set_state(AdminStates.editing_pinned_message)
    await callback.message.edit_text(
        "✏️ <b>Новое закрепленное сообщение</b>\n\n"
        "Пришлите текст, фото или видео, которое нужно закрепить.\n"
        "Бот отправит его всем активным пользователям, открепит старое и закрепит новое без уведомлений.",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="❌ Отмена", callback_data="admin_pinned_message")]
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@admin_required
@error_handler
async def process_pinned_message_update(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    media_type: Optional[str] = None
    media_file_id: Optional[str] = None

    if message.photo:
        media_type = "photo"
        media_file_id = message.photo[-1].file_id
    elif message.video:
        media_type = "video"
        media_file_id = message.video.file_id

    pinned_text = message.html_text or message.caption_html or message.text or message.caption or ""

    if not pinned_text and not media_file_id:
        await message.answer("❌ Не удалось прочитать текст или медиа в сообщении, попробуйте снова.")
        return

    try:
        pinned_message = await set_active_pinned_message(
            db,
            pinned_text,
            db_user.id,
            media_type=media_type,
            media_file_id=media_file_id,
        )
    except ValueError as validation_error:
        await message.answer(f"❌ {validation_error}")
        return

    await message.answer(
        "📌 Сообщение сохранено. Начинаю отправку и закрепление у пользователей...",
        parse_mode="HTML",
    )

    sent_count, failed_count = await broadcast_pinned_message(
        message.bot,
        db,
        pinned_message,
    )

    total = sent_count + failed_count
    await message.answer(
        "✅ <b>Закрепленное сообщение обновлено</b>\n\n"
        f"👥 Получателей: {total}\n"
        f"✅ Отправлено: {sent_count}\n"
        f"⚠️ Ошибок: {failed_count}",
        reply_markup=get_admin_messages_keyboard(db_user.language),
        parse_mode="HTML",
    )
    await state.clear()


@admin_required
@error_handler
async def show_broadcast_targets(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    await callback.message.edit_text(
        "🎯 <b>Выбор целевой аудитории</b>\n\n"
        "Выберите категорию пользователей для рассылки:",
        reply_markup=get_broadcast_target_keyboard(db_user.language),
        parse_mode="HTML" 
    )
    await callback.answer()


@admin_required
@error_handler
async def show_messages_history(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    page = 1
    if '_page_' in callback.data:
        page = int(callback.data.split('_page_')[1])
    
    limit = 10
    offset = (page - 1) * limit
    
    stmt = select(BroadcastHistory).order_by(BroadcastHistory.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(stmt)
    broadcasts = result.scalars().all()
    
    count_stmt = select(func.count(BroadcastHistory.id))
    count_result = await db.execute(count_stmt)
    total_count = count_result.scalar() or 0
    total_pages = (total_count + limit - 1) // limit
    
    if not broadcasts:
        text = """
📋 <b>История рассылок</b>

❌ История рассылок пуста.
Отправьте первую рассылку, чтобы увидеть её здесь.
"""
        keyboard = [[types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_messages")]]
    else:
        text = f"📋 <b>История рассылок</b> (страница {page}/{total_pages})\n\n"
        
        for broadcast in broadcasts:
            status_emoji = "✅" if broadcast.status == "completed" else "❌" if broadcast.status == "failed" else "⏳"
            success_rate = round((broadcast.sent_count / broadcast.total_count * 100), 1) if broadcast.total_count > 0 else 0
            
            message_preview = broadcast.message_text[:100] + "..." if len(broadcast.message_text) > 100 else broadcast.message_text
            
            import html
            message_preview = html.escape(message_preview) 
            
            text += f"""
{status_emoji} <b>{broadcast.created_at.strftime('%d.%m.%Y %H:%M')}</b>
📊 Отправлено: {broadcast.sent_count}/{broadcast.total_count} ({success_rate}%)
🎯 Аудитория: {get_target_name(broadcast.target_type)}
👤 Админ: {broadcast.admin_name}
📝 Сообщение: {message_preview}
━━━━━━━━━━━━━━━━━━━━━━━
"""
        
        keyboard = get_broadcast_history_keyboard(page, total_pages, db_user.language).inline_keyboard
    
    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )
    await callback.answer()


@admin_required
@error_handler
async def show_custom_broadcast(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    
    stats = await get_users_statistics(db)
    
    text = f"""
📝 <b>Рассылка по критериям</b>

📊 <b>Доступные фильтры:</b>

👥 <b>По регистрации:</b>
• Сегодня: {stats['today']} чел.
• За неделю: {stats['week']} чел.
• За месяц: {stats['month']} чел.

💼 <b>По активности:</b>
• Активные сегодня: {stats['active_today']} чел.
• Неактивные 7+ дней: {stats['inactive_week']} чел.
• Неактивные 30+ дней: {stats['inactive_month']} чел.

🔗 <b>По источнику:</b>
• Через рефералов: {stats['referrals']} чел.
• Прямая регистрация: {stats['direct']} чел.

Выберите критерий для фильтрации:
"""
    
    await callback.message.edit_text(
        text,
        reply_markup=get_custom_criteria_keyboard(db_user.language),
        parse_mode="HTML" 
    )
    await callback.answer()


@admin_required
@error_handler
async def select_custom_criteria(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    criteria = callback.data.replace('criteria_', '')
    
    criteria_names = {
        "today": "Зарегистрированные сегодня",
        "week": "Зарегистрированные за неделю",
        "month": "Зарегистрированные за месяц",
        "active_today": "Активные сегодня",
        "inactive_week": "Неактивные 7+ дней",
        "inactive_month": "Неактивные 30+ дней",
        "referrals": "Пришедшие через рефералов",
        "direct": "Прямая регистрация"
    }
    
    user_count = await get_custom_users_count(db, criteria)
    
    await state.update_data(broadcast_target=f"custom_{criteria}")
    
    await callback.message.edit_text(
        f"📨 <b>Создание рассылки</b>\n\n"
        f"🎯 <b>Критерий:</b> {criteria_names.get(criteria, criteria)}\n"
        f"👥 <b>Получателей:</b> {user_count}\n\n"
        f"Введите текст сообщения для рассылки:\n\n"
        f"<i>Поддерживается HTML разметка</i>",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="❌ Отмена", callback_data="admin_messages")]
        ]),
        parse_mode="HTML" 
    )
    
    await state.set_state(AdminStates.waiting_for_broadcast_message)
    await callback.answer()


@admin_required
@error_handler
async def select_broadcast_target(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    raw_target = callback.data[len("broadcast_"):]
    target_aliases = {
        "no_sub": "no",
    }
    target = target_aliases.get(raw_target, raw_target)

    target_names = {
        "all": "Всем пользователям",
        "active": "С активной подпиской",
        "trial": "С триальной подпиской",
        "no": "Без подписки",
        "expiring": "С истекающей подпиской",
        "expired": "С истекшей подпиской",
        "active_zero": "Активная подписка, трафик 0 ГБ",
        "trial_zero": "Триальная подписка, трафик 0 ГБ",
    }
    
    user_count = await get_target_users_count(db, target)
    
    await state.update_data(broadcast_target=target)
    
    await callback.message.edit_text(
        f"📨 <b>Создание рассылки</b>\n\n"
        f"🎯 <b>Аудитория:</b> {target_names.get(target, target)}\n"
        f"👥 <b>Получателей:</b> {user_count}\n\n"
        f"Введите текст сообщения для рассылки:\n\n"
        f"<i>Поддерживается HTML разметка</i>",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="❌ Отмена", callback_data="admin_messages")]
        ]),
        parse_mode="HTML" 
    )
    
    await state.set_state(AdminStates.waiting_for_broadcast_message)
    await callback.answer()


@admin_required
@error_handler
async def process_broadcast_message(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    broadcast_text = message.text
    
    if len(broadcast_text) > 4000:
        await message.answer("❌ Сообщение слишком длинное (максимум 4000 символов)")
        return
    
    await state.update_data(broadcast_message=broadcast_text)
    
    await message.answer(
        "🖼️ <b>Добавление медиафайла</b>\n\n"
        "Вы можете добавить к сообщению фото, видео или документ.\n"
        "Или пропустить этот шаг.\n\n"
        "Выберите тип медиа:",
        reply_markup=get_broadcast_media_keyboard(db_user.language),
        parse_mode="HTML"
    )

@admin_required
@error_handler
async def handle_media_selection(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    if callback.data == "skip_media":
        await state.update_data(has_media=False)
        await show_button_selector_callback(callback, db_user, state)
        return
    
    media_type = callback.data.replace('add_media_', '')
    
    media_instructions = {
        "photo": "📷 Отправьте фотографию для рассылки:",
        "video": "🎥 Отправьте видео для рассылки:",
        "document": "📄 Отправьте документ для рассылки:"
    }
    
    await state.update_data(
        media_type=media_type,
        waiting_for_media=True
    )
    
    await callback.message.edit_text(
        f"{media_instructions.get(media_type, 'Отправьте медиафайл:')}\n\n"
        f"<i>Размер файла не должен превышать 50 МБ</i>",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="❌ Отмена", callback_data="admin_messages")]
        ]),
        parse_mode="HTML"
    )
    
    await state.set_state(AdminStates.waiting_for_broadcast_media)
    await callback.answer()

@admin_required
@error_handler
async def process_broadcast_media(
    message: types.Message,
    db_user: User,
    state: FSMContext
):
    data = await state.get_data()
    expected_type = data.get('media_type')
    
    media_file_id = None
    media_type = None
    
    if message.photo and expected_type == "photo":
        media_file_id = message.photo[-1].file_id
        media_type = "photo"
    elif message.video and expected_type == "video":
        media_file_id = message.video.file_id
        media_type = "video"
    elif message.document and expected_type == "document":
        media_file_id = message.document.file_id
        media_type = "document"
    else:
        await message.answer(
            f"❌ Пожалуйста, отправьте {expected_type} как указано в инструкции."
        )
        return
    
    await state.update_data(
        has_media=True,
        media_file_id=media_file_id,
        media_type=media_type,
        media_caption=message.caption
    )
    
    await show_media_preview(message, db_user, state)

async def show_media_preview(
    message: types.Message,
    db_user: User,
    state: FSMContext
):
    data = await state.get_data()
    media_type = data.get('media_type')
    media_file_id = data.get('media_file_id')
    
    preview_text = f"🖼️ <b>Медиафайл добавлен</b>\n\n" \
                   f"📎 <b>Тип:</b> {media_type}\n" \
                   f"✅ Файл сохранен и готов к отправке\n\n" \
                   f"Что делать дальше?"
    
    # Для предпросмотра рассылки используем оригинальный метод без патчинга логотипа
    # чтобы показать именно загруженное фото
    from app.utils.message_patch import _original_answer
    
    if media_type == "photo" and media_file_id:
        # Показываем предпросмотр с загруженным фото
        await message.bot.send_photo(
            chat_id=message.chat.id,
            photo=media_file_id,
            caption=preview_text,
            reply_markup=get_media_confirm_keyboard(db_user.language),
            parse_mode="HTML"
        )
    else:
        # Для других типов медиа или если нет фото, используем обычное сообщение
        await _original_answer(message, preview_text, 
                             reply_markup=get_media_confirm_keyboard(db_user.language), 
                             parse_mode="HTML")

@admin_required
@error_handler
async def handle_media_confirmation(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    action = callback.data
    
    if action == "confirm_media":
        await show_button_selector_callback(callback, db_user, state)
    elif action == "replace_media":
        data = await state.get_data()
        media_type = data.get('media_type', 'photo')
        await handle_media_selection(callback, db_user, state)
    elif action == "skip_media":
        await state.update_data(
            has_media=False,
            media_file_id=None,
            media_type=None,
            media_caption=None
        )
        await show_button_selector_callback(callback, db_user, state)

@admin_required
@error_handler
async def handle_change_media(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    await callback.message.edit_text(
        "🖼️ <b>Изменение медиафайла</b>\n\n"
        "Выберите новый тип медиа:",
        reply_markup=get_broadcast_media_keyboard(db_user.language),
        parse_mode="HTML"
    )
    await callback.answer()

@admin_required
@error_handler
async def show_button_selector_callback(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    data = await state.get_data()
    has_media = data.get('has_media', False)
    selected_buttons = data.get('selected_buttons')

    if selected_buttons is None:
        selected_buttons = list(DEFAULT_SELECTED_BUTTONS)
        await state.update_data(selected_buttons=selected_buttons)
    
    media_info = ""
    if has_media:
        media_type = data.get('media_type', 'файл')
        media_info = f"\n🖼️ <b>Медиафайл:</b> {media_type} добавлен"
    
    text = f"""
📘 <b>Выбор дополнительных кнопок</b>

Выберите кнопки, которые будут добавлены к сообщению рассылки:

💰 <b>Пополнить баланс</b> — откроет методы пополнения
🤝 <b>Партнерка</b> — откроет реферальную программу
🎫 <b>Промокод</b> — откроет форму ввода промокода
🔗 <b>Подключиться</b> — поможет подключить приложение
📱 <b>Подписка</b> — покажет состояние подписки
🛠️ <b>Техподдержка</b> — свяжет с поддержкой

🏠 <b>Кнопка "На главную"</b> включена по умолчанию, но вы можете отключить её при необходимости.{media_info}

Выберите нужные кнопки и нажмите "Продолжить":
"""
    
    keyboard = get_updated_message_buttons_selector_keyboard_with_media(
        selected_buttons, has_media, db_user.language
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


@admin_required
@error_handler
async def show_button_selector(
    message: types.Message,
    db_user: User,
    state: FSMContext
):
    data = await state.get_data()
    selected_buttons = data.get('selected_buttons')
    if selected_buttons is None:
        selected_buttons = list(DEFAULT_SELECTED_BUTTONS)
        await state.update_data(selected_buttons=selected_buttons)

    has_media = data.get('has_media', False)

    text = """
📘 <b>Выбор дополнительных кнопок</b>

Выберите кнопки, которые будут добавлены к сообщению рассылки:

💰 <b>Пополнить баланс</b> — откроет методы пополнения
🤝 <b>Партнерка</b> — откроет реферальную программу
🎫 <b>Промокод</b> — откроет форму ввода промокода
🔗 <b>Подключиться</b> — поможет подключить приложение
📱 <b>Подписка</b> — покажет состояние подписки
🛠️ <b>Техподдержка</b> — свяжет с поддержкой

🏠 <b>Кнопка "На главную"</b> включена по умолчанию, но вы можете отключить её при необходимости.

Выберите нужные кнопки и нажмите "Продолжить":
"""

    keyboard = get_updated_message_buttons_selector_keyboard_with_media(
        selected_buttons, has_media, db_user.language
    )

    await message.answer(
        text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@admin_required
@error_handler
async def toggle_button_selection(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    button_type = callback.data.replace('btn_', '')
    data = await state.get_data()
    selected_buttons = data.get('selected_buttons')
    if selected_buttons is None:
        selected_buttons = list(DEFAULT_SELECTED_BUTTONS)
    else:
        selected_buttons = list(selected_buttons)

    if button_type in selected_buttons:
        selected_buttons.remove(button_type)
    else:
        selected_buttons.append(button_type)

    await state.update_data(selected_buttons=selected_buttons)

    has_media = data.get('has_media', False)
    keyboard = get_updated_message_buttons_selector_keyboard_with_media(
        selected_buttons, has_media, db_user.language
    )

    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def confirm_button_selection(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    data = await state.get_data()
    target = data.get('broadcast_target')
    message_text = data.get('broadcast_message')
    selected_buttons = data.get('selected_buttons')
    if selected_buttons is None:
        selected_buttons = list(DEFAULT_SELECTED_BUTTONS)
        await state.update_data(selected_buttons=selected_buttons)
    has_media = data.get('has_media', False)
    media_type = data.get('media_type')
    
    user_count = await get_target_users_count(db, target) if not target.startswith('custom_') else await get_custom_users_count(db, target.replace('custom_', ''))
    target_display = get_target_display_name(target)
    
    media_info = ""
    if has_media:
        media_type_names = {
            "photo": "Фотография",
            "video": "Видео",
            "document": "Документ"
        }
        media_info = f"\n🖼️ <b>Медиафайл:</b> {media_type_names.get(media_type, media_type)}"
    
    ordered_keys = [button_key for row in BUTTON_ROWS for button_key in row]
    button_labels = get_broadcast_button_labels(db_user.language)
    selected_names = [button_labels[key] for key in ordered_keys if key in selected_buttons]
    if selected_names:
        buttons_info = f"\n📘 <b>Кнопки:</b> {', '.join(selected_names)}"
    else:
        buttons_info = "\n📘 <b>Кнопки:</b> отсутствуют"
    
    preview_text = f"""
📨 <b>Предварительный просмотр рассылки</b>

🎯 <b>Аудитория:</b> {target_display}
👥 <b>Получателей:</b> {user_count}

📝 <b>Сообщение:</b>
{message_text}{media_info}

{buttons_info}

Подтвердить отправку?
"""
    
    keyboard = [
        [
            types.InlineKeyboardButton(text="✅ Отправить", callback_data="admin_confirm_broadcast"),
            types.InlineKeyboardButton(text="📘 Изменить кнопки", callback_data="edit_buttons")
        ]
    ]
    
    if has_media:
        keyboard.append([
            types.InlineKeyboardButton(text="🖼️ Изменить медиа", callback_data="change_media")
        ])
    
    keyboard.append([
        types.InlineKeyboardButton(text="❌ Отмена", callback_data="admin_messages")
    ])
    
    # Если есть медиа, показываем его с загруженным фото, иначе обычное текстовое сообщение
    if has_media and media_type == "photo":
        media_file_id = data.get('media_file_id')
        if media_file_id:
            # Удаляем текущее сообщение и отправляем новое с фото
            await callback.message.delete()
            await callback.bot.send_photo(
                chat_id=callback.message.chat.id,
                photo=media_file_id,
                caption=preview_text,
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
                parse_mode="HTML"
            )
        else:
            # Если нет file_id, используем обычное редактирование
            await callback.message.edit_text(
                preview_text,
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
                parse_mode="HTML"
            )
    else:
        # Для текстовых сообщений или других типов медиа используем обычное редактирование
        await callback.message.edit_text(
            preview_text,
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode="HTML"
        )
    
    await callback.answer()
@admin_required
@error_handler
async def confirm_broadcast(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    data = await state.get_data()
    target = data.get('broadcast_target')
    message_text = data.get('broadcast_message')
    selected_buttons = data.get('selected_buttons')
    if selected_buttons is None:
        selected_buttons = list(DEFAULT_SELECTED_BUTTONS)
    has_media = data.get('has_media', False)
    media_type = data.get('media_type')
    media_file_id = data.get('media_file_id')
    media_caption = data.get('media_caption')
    
    await callback.message.edit_text(
        "📨 Начинаю рассылку...\n\n"
        "⏳ Это может занять несколько минут.",
        reply_markup=None,
        parse_mode="HTML" 
    )
    
    if target.startswith('custom_'):
        users = await get_custom_users(db, target.replace('custom_', ''))
    else:
        users = await get_target_users(db, target)
    
    broadcast_history = BroadcastHistory(
        target_type=target,
        message_text=message_text,
        has_media=has_media,
        media_type=media_type,
        media_file_id=media_file_id,
        media_caption=media_caption,
        total_count=len(users),
        sent_count=0,
        failed_count=0,
        admin_id=db_user.id,
        admin_name=db_user.full_name,
        status="in_progress"
    )
    db.add(broadcast_history)
    await db.commit()
    await db.refresh(broadcast_history)
    
    sent_count = 0
    failed_count = 0
    
    broadcast_keyboard = create_broadcast_keyboard(selected_buttons, db_user.language)
    
    # Ограничение на количество одновременных отправок и базовая задержка между сообщениями,
    # чтобы избежать перегрузки бота и лимитов Telegram при больших рассылках
    max_concurrent_sends = 5
    per_message_delay = 0.05
    semaphore = asyncio.Semaphore(max_concurrent_sends)

    async def send_single_broadcast(user):
        """Отправляет одно сообщение рассылки с семафором ограничения"""
        async with semaphore:
            for attempt in range(3):
                try:
                    if has_media and media_file_id:
                        if media_type == "photo":
                            await callback.bot.send_photo(
                                chat_id=user.telegram_id,
                                photo=media_file_id,
                                caption=message_text,
                                parse_mode="HTML",
                                reply_markup=broadcast_keyboard
                            )
                        elif media_type == "video":
                            await callback.bot.send_video(
                                chat_id=user.telegram_id,
                                video=media_file_id,
                                caption=message_text,
                                parse_mode="HTML",
                                reply_markup=broadcast_keyboard
                            )
                        elif media_type == "document":
                            await callback.bot.send_document(
                                chat_id=user.telegram_id,
                                document=media_file_id,
                                caption=message_text,
                                parse_mode="HTML",
                                reply_markup=broadcast_keyboard
                            )
                    else:
                        await callback.bot.send_message(
                            chat_id=user.telegram_id,
                            text=message_text,
                            parse_mode="HTML",
                            reply_markup=broadcast_keyboard
                        )

                    await asyncio.sleep(per_message_delay)
                    return True, user.telegram_id
                except TelegramRetryAfter as e:
                    retry_delay = min(e.retry_after + 1, 30)
                    logger.warning(
                        f"Превышен лимит Telegram для {user.telegram_id}, ожидание {retry_delay} сек."
                    )
                    await asyncio.sleep(retry_delay)
                except TelegramForbiddenError:
                    # Пользователь мог удалить бота или запретить сообщения
                    logger.info(f"Рассылка недоступна для пользователя {user.telegram_id}: Forbidden")
                    return False, user.telegram_id
                except TelegramBadRequest as e:
                    logger.error(
                        f"Некорректный запрос при рассылке пользователю {user.telegram_id}: {e}"
                    )
                    return False, user.telegram_id
                except Exception as e:
                    logger.error(
                        f"Ошибка отправки рассылки пользователю {user.telegram_id} (попытка {attempt + 1}/3): {e}"
                    )
                    await asyncio.sleep(0.5 * (attempt + 1))

            return False, user.telegram_id

    # Отправляем сообщения пакетами для эффективности
    batch_size = 50
    for i in range(0, len(users), batch_size):
        batch = users[i:i + batch_size]
        tasks = [send_single_broadcast(user) for user in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, tuple):  # (success, telegram_id)
                success, _ = result
                if success:
                    sent_count += 1
                else:
                    failed_count += 1
            elif isinstance(result, Exception):
                failed_count += 1

        # Небольшая задержка между пакетами для снижения нагрузки на API
        await asyncio.sleep(0.25)
    
    status = "completed" if failed_count == 0 else "partial"
    await _persist_broadcast_result(
        db=db,
        broadcast_history=broadcast_history,
        sent_count=sent_count,
        failed_count=failed_count,
        status=status,
    )
    
    media_info = ""
    if has_media:
        media_info = f"\n🖼️ <b>Медиафайл:</b> {media_type}"
    
    result_text = f"""
✅ <b>Рассылка завершена!</b>

📊 <b>Результат:</b>
- Отправлено: {sent_count}
- Не доставлено: {failed_count}
- Всего пользователей: {len(users)}
- Успешность: {round(sent_count / len(users) * 100, 1) if users else 0}%{media_info}

<b>Администратор:</b> {db_user.full_name}
"""
    
    await callback.message.edit_text(
        result_text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="📨 К рассылкам", callback_data="admin_messages")]
        ]),
        parse_mode="HTML" 
    )
    
    await state.clear()
    logger.info(f"Рассылка выполнена админом {db_user.telegram_id}: {sent_count}/{len(users)} (медиа: {has_media})")


async def get_target_users_count(db: AsyncSession, target: str) -> int:
    users = await get_target_users(db, target)
    return len(users)


async def get_target_users(db: AsyncSession, target: str) -> list:
    # Загружаем всех активных пользователей батчами, чтобы не ограничиваться 10к
    users: list[User] = []
    offset = 0
    batch_size = 5000

    while True:
        batch = await get_users_list(
            db,
            offset=offset,
            limit=batch_size,
            status=UserStatus.ACTIVE,
        )

        if not batch:
            break

        users.extend(batch)
        offset += batch_size

    if target == "all":
        return users

    if target == "active":
        return [
            user
            for user in users
            if user.subscription
            and user.subscription.is_active
            and not user.subscription.is_trial
        ]

    if target == "trial":
        return [
            user
            for user in users
            if user.subscription and user.subscription.is_trial
        ]

    if target == "no":
        return [
            user
            for user in users
            if not user.subscription or not user.subscription.is_active
        ]

    if target == "expiring":
        expiring_subs = await get_expiring_subscriptions(db, 3)
        return [sub.user for sub in expiring_subs if sub.user]

    if target == "expired":
        now = datetime.utcnow()
        expired_statuses = {
            SubscriptionStatus.EXPIRED.value,
            SubscriptionStatus.DISABLED.value,
        }
        expired_users = []
        for user in users:
            subscription = user.subscription
            if subscription:
                if subscription.status in expired_statuses:
                    expired_users.append(user)
                    continue
                if subscription.end_date <= now and not subscription.is_active:
                    expired_users.append(user)
                    continue
            elif user.has_had_paid_subscription:
                expired_users.append(user)
        return expired_users

    if target == "active_zero":
        return [
            user
            for user in users
            if user.subscription
            and not user.subscription.is_trial
            and user.subscription.is_active
            and (user.subscription.traffic_used_gb or 0) <= 0
        ]

    if target == "trial_zero":
        return [
            user
            for user in users
            if user.subscription
            and user.subscription.is_trial
            and user.subscription.is_active
            and (user.subscription.traffic_used_gb or 0) <= 0
        ]

    if target == "zero":
        return [
            user
            for user in users
            if user.subscription
            and user.subscription.is_active
            and (user.subscription.traffic_used_gb or 0) <= 0
        ]

    if target == "expiring_subscribers":
        expiring_subs = await get_expiring_subscriptions(db, 7)
        return [sub.user for sub in expiring_subs if sub.user]

    if target == "expired_subscribers":
        now = datetime.utcnow()
        expired_statuses = {
            SubscriptionStatus.EXPIRED.value,
            SubscriptionStatus.DISABLED.value,
        }
        expired_users = []
        for user in users:
            subscription = user.subscription
            if subscription:
                if subscription.status in expired_statuses:
                    expired_users.append(user)
                    continue
                if subscription.end_date <= now and not subscription.is_active:
                    expired_users.append(user)
                    continue
            elif user.has_had_paid_subscription:
                expired_users.append(user)
        return expired_users

    if target == "canceled_subscribers":
        return [
            user
            for user in users
            if user.subscription
            and user.subscription.status == SubscriptionStatus.DISABLED.value
        ]

    if target == "trial_ending":
        now = datetime.utcnow()
        in_3_days = now + timedelta(days=3)
        return [
            user
            for user in users
            if user.subscription
            and user.subscription.is_trial
            and user.subscription.is_active
            and user.subscription.end_date <= in_3_days
        ]

    if target == "trial_expired":
        now = datetime.utcnow()
        return [
            user
            for user in users
            if user.subscription
            and user.subscription.is_trial
            and user.subscription.end_date <= now
        ]

    if target == "autopay_failed":
        from app.database.models import SubscriptionEvent
        week_ago = datetime.utcnow() - timedelta(days=7)
        stmt = select(SubscriptionEvent.user_id).where(
            and_(
                SubscriptionEvent.event_type == "autopay_failed",
                SubscriptionEvent.occurred_at >= week_ago,
            )
        ).distinct()
        result = await db.execute(stmt)
        failed_user_ids = set(result.scalars().all())
        return [user for user in users if user.id in failed_user_ids]

    if target == "low_balance":
        threshold_kopeks = 10000  # 100 рублей
        return [
            user
            for user in users
            if (user.balance_kopeks or 0) < threshold_kopeks
            and (user.balance_kopeks or 0) > 0
        ]

    if target == "inactive_30d":
        threshold = datetime.utcnow() - timedelta(days=30)
        return [
            user
            for user in users
            if user.last_activity and user.last_activity < threshold
        ]

    if target == "inactive_60d":
        threshold = datetime.utcnow() - timedelta(days=60)
        return [
            user
            for user in users
            if user.last_activity and user.last_activity < threshold
        ]

    if target == "inactive_90d":
        threshold = datetime.utcnow() - timedelta(days=90)
        return [
            user
            for user in users
            if user.last_activity and user.last_activity < threshold
        ]

    return []


async def get_custom_users_count(db: AsyncSession, criteria: str) -> int:
    users = await get_custom_users(db, criteria)
    return len(users)


async def get_custom_users(db: AsyncSession, criteria: str) -> list:
    now = datetime.utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)
    
    if criteria == "today":
        stmt = select(User).where(
            and_(User.status == "active", User.created_at >= today)
        )
    elif criteria == "week":
        stmt = select(User).where(
            and_(User.status == "active", User.created_at >= week_ago)
        )
    elif criteria == "month":
        stmt = select(User).where(
            and_(User.status == "active", User.created_at >= month_ago)
        )
    elif criteria == "active_today":
        stmt = select(User).where(
            and_(User.status == "active", User.last_activity >= today)
        )
    elif criteria == "inactive_week":
        stmt = select(User).where(
            and_(User.status == "active", User.last_activity < week_ago)
        )
    elif criteria == "inactive_month":
        stmt = select(User).where(
            and_(User.status == "active", User.last_activity < month_ago)
        )
    elif criteria == "referrals":
        stmt = select(User).where(
            and_(User.status == "active", User.referred_by_id.isnot(None))
        )
    elif criteria == "direct":
        stmt = select(User).where(
            and_(
                User.status == "active", 
                User.referred_by_id.is_(None)
            )
        )
    else:
        return []
    
    result = await db.execute(stmt)
    return result.scalars().all()


async def get_users_statistics(db: AsyncSession) -> dict:
    now = datetime.utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)
    
    stats = {}
    
    stats['today'] = await db.scalar(
        select(func.count(User.id)).where(
            and_(User.status == "active", User.created_at >= today)
        )
    ) or 0
    
    stats['week'] = await db.scalar(
        select(func.count(User.id)).where(
            and_(User.status == "active", User.created_at >= week_ago)
        )
    ) or 0
    
    stats['month'] = await db.scalar(
        select(func.count(User.id)).where(
            and_(User.status == "active", User.created_at >= month_ago)
        )
    ) or 0
    
    stats['active_today'] = await db.scalar(
        select(func.count(User.id)).where(
            and_(User.status == "active", User.last_activity >= today)
        )
    ) or 0
    
    stats['inactive_week'] = await db.scalar(
        select(func.count(User.id)).where(
            and_(User.status == "active", User.last_activity < week_ago)
        )
    ) or 0
    
    stats['inactive_month'] = await db.scalar(
        select(func.count(User.id)).where(
            and_(User.status == "active", User.last_activity < month_ago)
        )
    ) or 0
    
    stats['referrals'] = await db.scalar(
        select(func.count(User.id)).where(
            and_(User.status == "active", User.referred_by_id.isnot(None))
        )
    ) or 0
    
    stats['direct'] = await db.scalar(
        select(func.count(User.id)).where(
            and_(
                User.status == "active", 
                User.referred_by_id.is_(None)
            )
        )
    ) or 0
    
    return stats


def get_target_name(target_type: str) -> str:
    names = {
        "all": "Всем пользователям",
        "active": "С активной подпиской",
        "trial": "С триальной подпиской",
        "no": "Без подписки",
        "sub": "Без подписки",
        "expiring": "С истекающей подпиской",
        "expired": "С истекшей подпиской",
        "active_zero": "Активная подписка, трафик 0 ГБ",
        "trial_zero": "Триальная подписка, трафик 0 ГБ",
        "zero": "Подписка, трафик 0 ГБ",
        "custom_today": "Зарегистрированные сегодня",
        "custom_week": "Зарегистрированные за неделю",
        "custom_month": "Зарегистрированные за месяц",
        "custom_active_today": "Активные сегодня",
        "custom_inactive_week": "Неактивные 7+ дней",
        "custom_inactive_month": "Неактивные 30+ дней",
        "custom_referrals": "Через рефералов",
        "custom_direct": "Прямая регистрация"
    }
    return names.get(target_type, target_type)


def get_target_display_name(target: str) -> str:
    return get_target_name(target)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_messages_menu, F.data == "admin_messages")
    dp.callback_query.register(show_pinned_message_menu, F.data == "admin_pinned_message")
    dp.callback_query.register(prompt_pinned_message_update, F.data == "admin_pinned_message_edit")
    dp.callback_query.register(show_broadcast_targets, F.data.in_(["admin_msg_all", "admin_msg_by_sub"]))
    dp.callback_query.register(select_broadcast_target, F.data.startswith("broadcast_"))
    dp.callback_query.register(confirm_broadcast, F.data == "admin_confirm_broadcast")
    
    dp.callback_query.register(show_messages_history, F.data.startswith("admin_msg_history"))
    dp.callback_query.register(show_custom_broadcast, F.data == "admin_msg_custom")
    dp.callback_query.register(select_custom_criteria, F.data.startswith("criteria_"))
    
    dp.callback_query.register(toggle_button_selection, F.data.startswith("btn_"))
    dp.callback_query.register(confirm_button_selection, F.data == "buttons_confirm")
    dp.callback_query.register(show_button_selector_callback, F.data == "edit_buttons")
    dp.callback_query.register(handle_media_selection, F.data.startswith("add_media_"))
    dp.callback_query.register(handle_media_selection, F.data == "skip_media")
    dp.callback_query.register(handle_media_confirmation, F.data.in_(["confirm_media", "replace_media"]))
    dp.callback_query.register(handle_change_media, F.data == "change_media")
    dp.message.register(process_broadcast_message, AdminStates.waiting_for_broadcast_message)
    dp.message.register(process_broadcast_media, AdminStates.waiting_for_broadcast_media)
    dp.message.register(process_pinned_message_update, AdminStates.editing_pinned_message)
