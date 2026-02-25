import asyncio
import html
from datetime import datetime, timedelta

import structlog
from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import InterfaceError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import get_expiring_subscriptions
from app.database.crud.tariff import get_all_tariffs
from app.database.crud.user import get_users_list
from app.database.database import AsyncSessionLocal
from app.database.models import (
    BroadcastHistory,
    Subscription,
    SubscriptionStatus,
    User,
    UserStatus,
)
from app.keyboards.admin import (
    BROADCAST_BUTTON_ROWS,
    DEFAULT_BROADCAST_BUTTONS,
    get_admin_messages_keyboard,
    get_broadcast_button_config,
    get_broadcast_button_labels,
    get_broadcast_history_keyboard,
    get_broadcast_media_keyboard,
    get_broadcast_target_keyboard,
    get_custom_criteria_keyboard,
    get_media_confirm_keyboard,
    get_pinned_message_keyboard,
    get_updated_message_buttons_selector_keyboard_with_media,
)
from app.localization.texts import get_texts
from app.services.pinned_message_service import (
    broadcast_pinned_message,
    get_active_pinned_message,
    set_active_pinned_message,
    unpin_active_pinned_message,
)
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler
from app.utils.miniapp_buttons import BUTTON_KEY_TO_CABINET_PATH, build_miniapp_or_callback_button


logger = structlog.get_logger(__name__)


async def safe_edit_or_send_text(callback: types.CallbackQuery, text: str, reply_markup=None, parse_mode: str = 'HTML'):
    """
    Безопасно редактирует сообщение или удаляет и отправляет новое.
    Нужно для случаев, когда текущее сообщение - медиа (фото/видео),
    которое нельзя отредактировать через edit_text.
    """
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        if 'there is no text in the message to edit' in str(e):
            # Сообщение - медиа без текста, удаляем и отправляем новое
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.bot.send_message(
                chat_id=callback.message.chat.id, text=text, reply_markup=reply_markup, parse_mode=parse_mode
            )
        else:
            raise


BUTTON_ROWS = BROADCAST_BUTTON_ROWS
DEFAULT_SELECTED_BUTTONS = DEFAULT_BROADCAST_BUTTONS

CABINET_MINIAPP_BUTTON_KEYS = {
    'balance',
    'referrals',
    'promocode',
    'connect',
    'subscription',
    'support',
    'home',
}


def get_message_buttons_selector_keyboard(language: str = 'ru') -> types.InlineKeyboardMarkup:
    return get_updated_message_buttons_selector_keyboard(list(DEFAULT_SELECTED_BUTTONS), language)


def get_updated_message_buttons_selector_keyboard(
    selected_buttons: list, language: str = 'ru'
) -> types.InlineKeyboardMarkup:
    return get_updated_message_buttons_selector_keyboard_with_media(selected_buttons, False, language)


def create_broadcast_keyboard(selected_buttons: list, language: str = 'ru') -> types.InlineKeyboardMarkup | None:
    selected_buttons = selected_buttons or []
    keyboard: list[list[types.InlineKeyboardButton]] = []
    button_config_map = get_broadcast_button_config(language)

    for row in BUTTON_ROWS:
        row_buttons: list[types.InlineKeyboardButton] = []
        for button_key in row:
            if button_key not in selected_buttons:
                continue
            button_config = button_config_map[button_key]
            if settings.is_cabinet_mode() and button_key in CABINET_MINIAPP_BUTTON_KEYS:
                row_buttons.append(
                    build_miniapp_or_callback_button(
                        text=button_config['text'],
                        callback_data=button_config['callback'],
                        cabinet_path=BUTTON_KEY_TO_CABINET_PATH.get(button_key, ''),
                    )
                )
            else:
                row_buttons.append(
                    types.InlineKeyboardButton(text=button_config['text'], callback_data=button_config['callback'])
                )
        if row_buttons:
            keyboard.append(row_buttons)

    if not keyboard:
        return None

    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


async def _persist_broadcast_result(
    broadcast_id: int,
    sent_count: int,
    failed_count: int,
    status: str,
) -> None:
    """
    Сохраняет результаты рассылки в НОВОЙ сессии.

    ВАЖНО: Используем свежую сессию вместо переданной, потому что за время
    долгой рассылки (минуты/часы) оригинальное соединение гарантированно
    закроется по таймауту PostgreSQL (idle_in_transaction_session_timeout).

    Args:
        broadcast_id: ID записи BroadcastHistory (не ORM-объект!)
        sent_count: Количество успешно отправленных сообщений
        failed_count: Количество неудачных отправок
        status: Финальный статус рассылки ('completed', 'partial', 'failed')
    """
    completed_at = datetime.utcnow()
    max_retries = 3
    retry_delay = 1.0

    for attempt in range(1, max_retries + 1):
        try:
            async with AsyncSessionLocal() as session:
                broadcast_history = await session.get(BroadcastHistory, broadcast_id)
                if not broadcast_history:
                    logger.critical(
                        'Не удалось найти запись BroadcastHistory # для записи результатов', broadcast_id=broadcast_id
                    )
                    return

                broadcast_history.sent_count = sent_count
                broadcast_history.failed_count = failed_count
                broadcast_history.status = status
                broadcast_history.completed_at = completed_at
                await session.commit()

                logger.info(
                    'Результаты рассылки сохранены (id sent failed status=)',
                    broadcast_id=broadcast_id,
                    sent_count=sent_count,
                    failed_count=failed_count,
                    status=status,
                )
                return

        except InterfaceError as error:
            logger.warning(
                'Ошибка соединения при сохранении результатов рассылки (попытка /)',
                attempt=attempt,
                max_retries=max_retries,
                error=error,
            )
            if attempt < max_retries:
                await asyncio.sleep(retry_delay)
                retry_delay *= 2
            else:
                logger.critical(
                    'Не удалось сохранить результаты рассылки после попыток (id=)',
                    max_retries=max_retries,
                    broadcast_id=broadcast_id,
                )

        except Exception as error:
            logger.critical(
                'Неожиданная ошибка при сохранении результатов рассылки (id=)',
                broadcast_id=broadcast_id,
                exc_info=error,
            )
            return


@admin_required
@error_handler
async def show_messages_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    text = texts.t(
        'ADMIN_MESSAGES_MENU_TEXT',
        '📨 <b>Управление рассылками</b>\n\n'
        'Выберите тип рассылки:\n\n'
        '- <b>Всем пользователям</b> - рассылка всем активным пользователям\n'
        '- <b>По подпискам</b> - фильтрация по типу подписки\n'
        '- <b>По критериям</b> - настраиваемые фильтры\n'
        '- <b>История</b> - просмотр предыдущих рассылок\n\n'
        '⚠️ Будьте осторожны с массовыми рассылками!',
    )

    await safe_edit_or_send_text(
        callback, text, reply_markup=get_admin_messages_keyboard(db_user.language), parse_mode='HTML'
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
    texts = get_texts(db_user.language)
    await state.clear()
    pinned_message = await get_active_pinned_message(db)

    if pinned_message:
        content_preview = html.escape(pinned_message.content or '')
        last_updated = pinned_message.updated_at or pinned_message.created_at
        timestamp_text = last_updated.strftime('%d.%m.%Y %H:%M') if last_updated else '—'
        media_line = ''
        if pinned_message.media_type:
            media_label = (
                texts.t('ADMIN_PINNED_MEDIA_PHOTO', 'Фото')
                if pinned_message.media_type == 'photo'
                else texts.t('ADMIN_MESSAGES_MEDIA_TYPE_VIDEO', 'Видео')
            )
            media_line = texts.t('ADMIN_PINNED_MEDIA_LINE', '📎 Медиа: {media_label}\n').format(media_label=media_label)
        position_line = (
            texts.t('ADMIN_PINNED_POSITION_BEFORE', '⬆️ Показать перед меню')
            if pinned_message.send_before_menu
            else texts.t('ADMIN_PINNED_POSITION_AFTER', '⬇️ Показать после меню')
        )
        start_mode_line = (
            texts.t('ADMIN_PINNED_START_EVERY_TIME', '🔁 Показать при каждом /start')
            if pinned_message.send_on_every_start
            else texts.t('ADMIN_PINNED_START_ONCE', '🚫 Показывать только один раз')
        )
        updated_line = texts.t('ADMIN_PINNED_UPDATED_AT_LINE', '🕒 Обновлено: {timestamp}').format(
            timestamp=timestamp_text
        )
        body = texts.t(
            'ADMIN_PINNED_MENU_WITH_CONTENT',
            '📌 <b>Закрепленное сообщение</b>\n\n'
            '📝 Текущий текст:\n'
            '<code>{content}</code>\n\n'
            '{media_line}{position_line}\n{start_mode_line}\n{updated_line}',
        ).format(
            content=content_preview,
            media_line=media_line,
            position_line=position_line,
            start_mode_line=start_mode_line,
            updated_line=updated_line,
        )
    else:
        body = texts.t(
            'ADMIN_PINNED_MENU_EMPTY',
            '📌 <b>Закрепленное сообщение</b>\n\n'
            'Сообщение не задано. Отправьте новый текст, чтобы разослать и закрепить его у пользователей.',
        )

    await callback.message.edit_text(
        body,
        reply_markup=get_pinned_message_keyboard(
            db_user.language,
            send_before_menu=getattr(pinned_message, 'send_before_menu', True),
            send_on_every_start=getattr(pinned_message, 'send_on_every_start', True),
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def prompt_pinned_message_update(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    await state.set_state(AdminStates.editing_pinned_message)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_PINNED_UPDATE_PROMPT',
            '✏️ <b>Новое закрепленное сообщение</b>\n\n'
            'Пришлите текст, фото или видео, которое нужно закрепить.\n'
            'Бот отправит его всем активным пользователям, открепит старое и закрепит новое без уведомлений.',
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=texts.t('ADMIN_CANCEL', '❌ Отмена'), callback_data='admin_pinned_message')]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_pinned_message_position(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    pinned_message = await get_active_pinned_message(db)
    if not pinned_message:
        await callback.answer(
            texts.t('ADMIN_PINNED_SET_FIRST_ALERT', 'Сначала задайте закрепленное сообщение'),
            show_alert=True,
        )
        return

    pinned_message.send_before_menu = not pinned_message.send_before_menu
    pinned_message.updated_at = datetime.utcnow()
    await db.commit()

    await show_pinned_message_menu(callback, db_user, db, state)


@admin_required
@error_handler
async def toggle_pinned_message_start_mode(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    pinned_message = await get_active_pinned_message(db)
    if not pinned_message:
        await callback.answer(
            texts.t('ADMIN_PINNED_SET_FIRST_ALERT', 'Сначала задайте закрепленное сообщение'),
            show_alert=True,
        )
        return

    pinned_message.send_on_every_start = not pinned_message.send_on_every_start
    pinned_message.updated_at = datetime.utcnow()
    await db.commit()

    await show_pinned_message_menu(callback, db_user, db, state)


@admin_required
@error_handler
async def delete_pinned_message(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    pinned_message = await get_active_pinned_message(db)
    if not pinned_message:
        await callback.answer(
            texts.t('ADMIN_PINNED_ALREADY_MISSING_ALERT', 'Закрепленное сообщение уже отсутствует'),
            show_alert=True,
        )
        return

    await callback.message.edit_text(
        texts.t(
            'ADMIN_PINNED_DELETE_IN_PROGRESS',
            '🗑️ <b>Удаление закрепленного сообщения</b>\n\n'
            'Подождите, пока бот открепит сообщение у пользователей...',
        ),
        parse_mode='HTML',
    )

    unpinned_count, failed_count, deleted = await unpin_active_pinned_message(
        callback.bot,
        db,
    )

    if not deleted:
        await callback.message.edit_text(
            texts.t(
                'ADMIN_PINNED_DELETE_NOT_FOUND',
                '❌ Не удалось найти активное закрепленное сообщение для удаления',
            ),
            reply_markup=get_admin_messages_keyboard(db_user.language),
            parse_mode='HTML',
        )
        await state.clear()
        return

    total = unpinned_count + failed_count
    await callback.message.edit_text(
        texts.t(
            'ADMIN_PINNED_DELETED_STATS',
            '✅ <b>Закрепленное сообщение удалено</b>\n\n'
            '👥 Чатов обработано: {total}\n'
            '✅ Откреплено: {unpinned_count}\n'
            '⚠️ Ошибок: {failed_count}\n\n'
            'Новое сообщение можно задать кнопкой "Обновить".',
        ).format(
            total=total,
            unpinned_count=unpinned_count,
            failed_count=failed_count,
        ),
        reply_markup=get_admin_messages_keyboard(db_user.language),
        parse_mode='HTML',
    )
    await state.clear()


@admin_required
@error_handler
async def process_pinned_message_update(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    media_type: str | None = None
    media_file_id: str | None = None

    if message.photo:
        media_type = 'photo'
        media_file_id = message.photo[-1].file_id
    elif message.video:
        media_type = 'video'
        media_file_id = message.video.file_id

    pinned_text = message.html_text or message.caption_html or message.text or message.caption or ''

    if not pinned_text and not media_file_id:
        await message.answer(
            texts.t('ADMIN_PINNED_NO_CONTENT', '❌ Не удалось прочитать текст или медиа в сообщении, попробуйте снова.')
        )
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
        await message.answer(f'❌ {validation_error}')
        return

    # Сообщение сохранено, спрашиваем о рассылке
    from app.keyboards.admin import get_pinned_broadcast_confirm_keyboard
    from app.states import AdminStates

    await message.answer(
        texts.t(
            'ADMIN_PINNED_SAVED_ASK_BROADCAST',
            '📌 <b>Сообщение сохранено!</b>\n\n'
            'Выберите, как доставить сообщение пользователям:\n\n'
            '• <b>Разослать сейчас</b> — отправит и закрепит у всех активных пользователей\n'
            '• <b>Только при /start</b> — пользователи увидят при следующем запуске бота',
        ),
        reply_markup=get_pinned_broadcast_confirm_keyboard(db_user.language, pinned_message.id),
        parse_mode='HTML',
    )
    await state.set_state(AdminStates.confirming_pinned_broadcast)


@admin_required
@error_handler
async def handle_pinned_broadcast_now(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Разослать закреплённое сообщение сейчас всем пользователям."""
    texts = get_texts(db_user.language)

    # Получаем ID сообщения из callback_data
    pinned_message_id = int(callback.data.split(':')[1])

    # Получаем сообщение из БД
    from sqlalchemy import select

    from app.database.models import PinnedMessage

    result = await db.execute(select(PinnedMessage).where(PinnedMessage.id == pinned_message_id))
    pinned_message = result.scalar_one_or_none()

    if not pinned_message:
        await callback.answer(texts.t('ADMIN_MESSAGES_NOT_FOUND', '❌ Сообщение не найдено'), show_alert=True)
        await state.clear()
        return

    await callback.message.edit_text(
        texts.t('ADMIN_PINNED_SAVING', '📌 Сообщение сохранено. Начинаю отправку и закрепление у пользователей...'),
        parse_mode='HTML',
    )

    sent_count, failed_count = await broadcast_pinned_message(
        callback.bot,
        db,
        pinned_message,
    )

    total = sent_count + failed_count
    await callback.message.edit_text(
        texts.t(
            'ADMIN_PINNED_UPDATED',
            '✅ <b>Закрепленное сообщение обновлено</b>\n\n'
            '👥 Получателей: {total}\n'
            '✅ Отправлено: {sent}\n'
            '⚠️ Ошибок: {failed}',
        ).format(total=total, sent=sent_count, failed=failed_count),
        reply_markup=get_admin_messages_keyboard(db_user.language),
        parse_mode='HTML',
    )
    await state.clear()


@admin_required
@error_handler
async def handle_pinned_broadcast_skip(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Пропустить рассылку — пользователи увидят при /start."""
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_PINNED_SAVED_NO_BROADCAST',
            '✅ <b>Закрепленное сообщение сохранено</b>\n\n'
            'Рассылка не выполнена. Пользователи увидят сообщение при следующем вводе /start.',
        ),
        reply_markup=get_admin_messages_keyboard(db_user.language),
        parse_mode='HTML',
    )
    await state.clear()


@admin_required
@error_handler
async def show_broadcast_targets(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_MESSAGES_TARGET_SELECT',
            '🎯 <b>Выбор целевой аудитории</b>\n\n'
            'Выберите категорию пользователей для рассылки:',
        ),
        reply_markup=get_broadcast_target_keyboard(db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_tariff_filter(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Показывает список тарифов для фильтрации рассылки."""
    texts = get_texts(db_user.language)
    tariffs = await get_all_tariffs(db, include_inactive=False)

    if not tariffs:
        await callback.message.edit_text(
            texts.t(
                'ADMIN_MESSAGES_NO_TARIFFS',
                '❌ <b>Нет доступных тарифов</b>\n\nСоздайте тарифы в разделе управления тарифами.',
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text=texts.t('BACK', '⬅️ Назад'), callback_data='admin_msg_by_sub')]
                ]
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    # Получаем количество подписчиков на каждом тарифе
    tariff_counts = {}
    for tariff in tariffs:
        count_query = select(func.count(Subscription.id)).where(
            Subscription.tariff_id == tariff.id,
            Subscription.status == SubscriptionStatus.ACTIVE.value,
        )
        result = await db.execute(count_query)
        tariff_counts[tariff.id] = result.scalar() or 0

    buttons = []
    for tariff in tariffs:
        count = tariff_counts.get(tariff.id, 0)
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MESSAGES_TARIFF_BUTTON',
                        '{tariff_name} ({count} чел.)',
                    ).format(tariff_name=tariff.name, count=count),
                    callback_data=f'broadcast_tariff_{tariff.id}',
                )
            ]
        )

    buttons.append([types.InlineKeyboardButton(text=texts.t('BACK', '⬅️ Назад'), callback_data='admin_msg_by_sub')])

    await callback.message.edit_text(
        texts.t(
            'ADMIN_MESSAGES_TARIFF_SELECT',
            '📦 <b>Рассылка по тарифу</b>\n\n'
            'Выберите тариф для рассылки пользователям с активной подпиской на этот тариф:',
        ),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_messages_history(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
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
        text = texts.t(
            'ADMIN_MESSAGES_HISTORY_EMPTY',
            '📋 <b>История рассылок</b>\n\n'
            '❌ История рассылок пуста.\n'
            'Отправьте первую рассылку, чтобы увидеть её здесь.',
        )
        keyboard = [[types.InlineKeyboardButton(text=texts.t('BACK', '⬅️ Назад'), callback_data='admin_messages')]]
    else:
        text = texts.t(
            'ADMIN_MESSAGES_HISTORY_HEADER',
            '📋 <b>История рассылок</b> (страница {page}/{total_pages})\n\n',
        ).format(page=page, total_pages=total_pages)

        for broadcast in broadcasts:
            status_emoji = '✅' if broadcast.status == 'completed' else '❌' if broadcast.status == 'failed' else '⏳'
            success_rate = (
                round((broadcast.sent_count / broadcast.total_count * 100), 1) if broadcast.total_count > 0 else 0
            )

            message_preview = (
                broadcast.message_text[:100] + '...' if len(broadcast.message_text) > 100 else broadcast.message_text
            )

            import html

            message_preview = html.escape(message_preview)

            text += texts.t(
                'ADMIN_MESSAGES_HISTORY_ITEM',
                '{status_emoji} <b>{date}</b>\n'
                '📊 Отправлено: {sent_count}/{total_count} ({success_rate}%)\n'
                '🎯 Аудитория: {target_name}\n'
                '👤 Админ: {admin_name}\n'
                '📝 Сообщение: {message_preview}\n'
                '━━━━━━━━━━━━━━━━━━━━━━━\n',
            ).format(
                status_emoji=status_emoji,
                date=broadcast.created_at.strftime('%d.%m.%Y %H:%M'),
                sent_count=broadcast.sent_count,
                total_count=broadcast.total_count,
                success_rate=success_rate,
                target_name=get_target_name(broadcast.target_type, db_user.language),
                admin_name=broadcast.admin_name,
                message_preview=message_preview,
            )

        keyboard = get_broadcast_history_keyboard(page, total_pages, db_user.language).inline_keyboard

    await callback.message.edit_text(
        text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def show_custom_broadcast(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    stats = await get_users_statistics(db)

    text = texts.t(
        'ADMIN_MESSAGES_CUSTOM_FILTERS',
        '📝 <b>Рассылка по критериям</b>\n\n'
        '📊 <b>Доступные фильтры:</b>\n\n'
        '👥 <b>По регистрации:</b>\n'
        '• Сегодня: {today} чел.\n'
        '• За неделю: {week} чел.\n'
        '• За месяц: {month} чел.\n\n'
        '💼 <b>По активности:</b>\n'
        '• Активные сегодня: {active_today} чел.\n'
        '• Неактивные 7+ дней: {inactive_week} чел.\n'
        '• Неактивные 30+ дней: {inactive_month} чел.\n\n'
        '🔗 <b>По источнику:</b>\n'
        '• Через рефералов: {referrals} чел.\n'
        '• Прямая регистрация: {direct} чел.\n\n'
        'Выберите критерий для фильтрации:',
    ).format(
        today=stats['today'],
        week=stats['week'],
        month=stats['month'],
        active_today=stats['active_today'],
        inactive_week=stats['inactive_week'],
        inactive_month=stats['inactive_month'],
        referrals=stats['referrals'],
        direct=stats['direct'],
    )

    await callback.message.edit_text(
        text, reply_markup=get_custom_criteria_keyboard(db_user.language), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def select_custom_criteria(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    criteria = callback.data.replace('criteria_', '')
    criteria_name = get_target_name(f'custom_{criteria}', db_user.language)

    user_count = await get_custom_users_count(db, criteria)

    await state.update_data(broadcast_target=f'custom_{criteria}')

    await callback.message.edit_text(
        texts.t(
            'ADMIN_MESSAGES_CREATE_BY_CRITERIA',
            '📨 <b>Создание рассылки</b>\n\n'
            '🎯 <b>Критерий:</b> {criteria_name}\n'
            '👥 <b>Получателей:</b> {user_count}\n\n'
            'Введите текст сообщения для рассылки:\n\n'
            '<i>Поддерживается HTML разметка</i>',
        ).format(criteria_name=criteria_name, user_count=user_count),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=texts.t('ADMIN_CANCEL', '❌ Отмена'), callback_data='admin_messages')]]
        ),
        parse_mode='HTML',
    )

    await state.set_state(AdminStates.waiting_for_broadcast_message)
    await callback.answer()


@admin_required
@error_handler
async def select_broadcast_target(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    raw_target = callback.data[len('broadcast_') :]
    target_aliases = {
        'no_sub': 'no',
    }
    target = target_aliases.get(raw_target, raw_target)

    # Обработка фильтра по тарифу
    target_name = get_target_name(target, db_user.language)
    if target.startswith('tariff_'):
        tariff_id = int(target.split('_')[1])
        from app.database.crud.tariff import get_tariff_by_id

        tariff = await get_tariff_by_id(db, tariff_id)
        if tariff:
            target_name = texts.t('ADMIN_MESSAGES_TARGET_TARIFF_NAMED', 'Тариф «{name}»').format(name=tariff.name)
        else:
            target_name = texts.t('ADMIN_MESSAGES_TARGET_TARIFF_ID', 'Тариф #{tariff_id}').format(tariff_id=tariff_id)

    user_count = await get_target_users_count(db, target)

    await state.update_data(broadcast_target=target)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_MESSAGES_CREATE_BY_TARGET',
            '📨 <b>Создание рассылки</b>\n\n'
            '🎯 <b>Аудитория:</b> {target_name}\n'
            '👥 <b>Получателей:</b> {user_count}\n\n'
            'Введите текст сообщения для рассылки:\n\n'
            '<i>Поддерживается HTML разметка</i>',
        ).format(target_name=target_name, user_count=user_count),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=texts.t('ADMIN_CANCEL', '❌ Отмена'), callback_data='admin_messages')]]
        ),
        parse_mode='HTML',
    )

    await state.set_state(AdminStates.waiting_for_broadcast_message)
    await callback.answer()


@admin_required
@error_handler
async def process_broadcast_message(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    broadcast_text = message.text

    if len(broadcast_text) > 4000:
        await message.answer(
            texts.t('ADMIN_MESSAGES_TEXT_TOO_LONG', '❌ Сообщение слишком длинное (максимум 4000 символов)')
        )
        return

    await state.update_data(broadcast_message=broadcast_text)

    await message.answer(
        texts.t(
            'ADMIN_MESSAGES_ADD_MEDIA_PROMPT',
            '🖼️ <b>Добавление медиафайла</b>\n\n'
            'Вы можете добавить к сообщению фото, видео или документ.\n'
            'Или пропустить этот шаг.\n\n'
            'Выберите тип медиа:',
        ),
        reply_markup=get_broadcast_media_keyboard(db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def handle_media_selection(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    if callback.data == 'skip_media':
        await state.update_data(has_media=False)
        await show_button_selector_callback(callback, db_user, state)
        return

    media_type = callback.data.replace('add_media_', '')

    media_instructions = {
        'photo': texts.t('ADMIN_MESSAGES_MEDIA_PROMPT_PHOTO', '📷 Отправьте фотографию для рассылки:'),
        'video': texts.t('ADMIN_MESSAGES_MEDIA_PROMPT_VIDEO', '🎥 Отправьте видео для рассылки:'),
        'document': texts.t('ADMIN_MESSAGES_MEDIA_PROMPT_DOCUMENT', '📄 Отправьте документ для рассылки:'),
    }

    await state.update_data(media_type=media_type, waiting_for_media=True)

    instruction_text = (
        texts.t(
            'ADMIN_MESSAGES_MEDIA_INSTRUCTION_WITH_LIMIT',
            '{instruction}\n\n<i>Размер файла не должен превышать 50 МБ</i>',
        ).format(
            instruction=media_instructions.get(
                media_type,
                texts.t('ADMIN_MESSAGES_MEDIA_PROMPT_DEFAULT', 'Отправьте медиафайл:'),
            )
        )
    )
    instruction_keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.t('ADMIN_CANCEL', '❌ Отмена'), callback_data='admin_messages')]]
    )

    # Проверяем, является ли текущее сообщение медиа-сообщением
    is_media_message = (
        callback.message.photo
        or callback.message.video
        or callback.message.document
        or callback.message.animation
        or callback.message.audio
        or callback.message.voice
    )

    if is_media_message:
        # Удаляем медиа-сообщение и отправляем новое текстовое
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(instruction_text, reply_markup=instruction_keyboard, parse_mode='HTML')
    else:
        await callback.message.edit_text(instruction_text, reply_markup=instruction_keyboard, parse_mode='HTML')

    await state.set_state(AdminStates.waiting_for_broadcast_media)
    await callback.answer()


@admin_required
@error_handler
async def process_broadcast_media(message: types.Message, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    expected_type = data.get('media_type')

    media_file_id = None
    media_type = None

    if message.photo and expected_type == 'photo':
        media_file_id = message.photo[-1].file_id
        media_type = 'photo'
    elif message.video and expected_type == 'video':
        media_file_id = message.video.file_id
        media_type = 'video'
    elif message.document and expected_type == 'document':
        media_file_id = message.document.file_id
        media_type = 'document'
    else:
        await message.answer(
            texts.t(
                'ADMIN_MESSAGES_MEDIA_TYPE_MISMATCH',
                '❌ Пожалуйста, отправьте {expected_type} как указано в инструкции.',
            ).format(expected_type=expected_type)
        )
        return

    await state.update_data(
        has_media=True, media_file_id=media_file_id, media_type=media_type, media_caption=message.caption
    )

    await show_media_preview(message, db_user, state)


async def show_media_preview(message: types.Message, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    media_type = data.get('media_type')
    media_file_id = data.get('media_file_id')
    media_type_names = {
        'photo': texts.t('ADMIN_MESSAGES_MEDIA_TYPE_PHOTO', 'Фотография'),
        'video': texts.t('ADMIN_MESSAGES_MEDIA_TYPE_VIDEO', 'Видео'),
        'document': texts.t('ADMIN_MESSAGES_MEDIA_TYPE_DOCUMENT', 'Документ'),
    }
    media_type_display = media_type_names.get(media_type, media_type)

    preview_text = texts.t(
        'ADMIN_MESSAGES_MEDIA_PREVIEW',
        '🖼️ <b>Медиафайл добавлен</b>\n\n'
        '📎 <b>Тип:</b> {media_type}\n'
        '✅ Файл сохранен и готов к отправке\n\n'
        'Что делать дальше?',
    ).format(media_type=media_type_display)

    # Для предпросмотра рассылки используем оригинальный метод без патчинга логотипа
    # чтобы показать именно загруженное фото
    from app.utils.message_patch import _original_answer

    if media_type == 'photo' and media_file_id:
        # Показываем предпросмотр с загруженным фото
        await message.bot.send_photo(
            chat_id=message.chat.id,
            photo=media_file_id,
            caption=preview_text,
            reply_markup=get_media_confirm_keyboard(db_user.language),
            parse_mode='HTML',
        )
    else:
        # Для других типов медиа или если нет фото, используем обычное сообщение
        await _original_answer(
            message, preview_text, reply_markup=get_media_confirm_keyboard(db_user.language), parse_mode='HTML'
        )


@admin_required
@error_handler
async def handle_media_confirmation(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    action = callback.data

    if action == 'confirm_media':
        await show_button_selector_callback(callback, db_user, state)
    elif action == 'replace_media':
        data = await state.get_data()
        data.get('media_type', 'photo')
        await handle_media_selection(callback, db_user, state)
    elif action == 'skip_media':
        await state.update_data(has_media=False, media_file_id=None, media_type=None, media_caption=None)
        await show_button_selector_callback(callback, db_user, state)


@admin_required
@error_handler
async def handle_change_media(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    await safe_edit_or_send_text(
        callback,
        texts.t(
            'ADMIN_MESSAGES_CHANGE_MEDIA_PROMPT',
            '🖼️ <b>Изменение медиафайла</b>\n\nВыберите новый тип медиа:',
        ),
        reply_markup=get_broadcast_media_keyboard(db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_button_selector_callback(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    has_media = data.get('has_media', False)
    selected_buttons = data.get('selected_buttons')

    if selected_buttons is None:
        selected_buttons = list(DEFAULT_SELECTED_BUTTONS)
        await state.update_data(selected_buttons=selected_buttons)

    media_info = ''
    if has_media:
        media_type = data.get('media_type', texts.t('ADMIN_MESSAGES_MEDIA_TYPE_FILE', 'файл'))
        media_info = texts.t(
            'ADMIN_MESSAGES_BUTTONS_MEDIA_INFO',
            '\n🖼️ <b>Медиафайл:</b> {media_type} добавлен',
        ).format(media_type=media_type)

    text = texts.t(
        'ADMIN_MESSAGES_BUTTONS_SELECTOR_TEXT',
        '📘 <b>Выбор дополнительных кнопок</b>\n\n'
        'Выберите кнопки, которые будут добавлены к сообщению рассылки:\n\n'
        '💰 <b>Пополнить баланс</b> — откроет методы пополнения\n'
        '🤝 <b>Партнерка</b> — откроет реферальную программу\n'
        '🎫 <b>Промокод</b> — откроет форму ввода промокода\n'
        '🔗 <b>Подключиться</b> — поможет подключить приложение\n'
        '📱 <b>Подписка</b> — покажет состояние подписки\n'
        '🛠️ <b>Техподдержка</b> — свяжет с поддержкой\n\n'
        '🏠 <b>Кнопка "На главную"</b> включена по умолчанию, но вы можете отключить её '
        'при необходимости.{media_info}\n\n'
        'Выберите нужные кнопки и нажмите "Продолжить":',
    ).format(media_info=media_info)

    keyboard = get_updated_message_buttons_selector_keyboard_with_media(selected_buttons, has_media, db_user.language)

    # Проверяем, является ли текущее сообщение медиа-сообщением
    # (фото, видео, документ и т.д.) - для них нельзя использовать edit_text
    is_media_message = (
        callback.message.photo
        or callback.message.video
        or callback.message.document
        or callback.message.animation
        or callback.message.audio
        or callback.message.voice
    )

    if is_media_message:
        # Удаляем медиа-сообщение и отправляем новое текстовое
        try:
            await callback.message.delete()
        except Exception:
            pass  # Игнорируем ошибки удаления
        await callback.message.answer(text, reply_markup=keyboard, parse_mode='HTML')
    else:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def show_button_selector(message: types.Message, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    selected_buttons = data.get('selected_buttons')
    if selected_buttons is None:
        selected_buttons = list(DEFAULT_SELECTED_BUTTONS)
        await state.update_data(selected_buttons=selected_buttons)

    has_media = data.get('has_media', False)

    text = texts.t(
        'ADMIN_MESSAGES_BUTTONS_SELECTOR_TEXT',
        '📘 <b>Выбор дополнительных кнопок</b>\n\n'
        'Выберите кнопки, которые будут добавлены к сообщению рассылки:\n\n'
        '💰 <b>Пополнить баланс</b> — откроет методы пополнения\n'
        '🤝 <b>Партнерка</b> — откроет реферальную программу\n'
        '🎫 <b>Промокод</b> — откроет форму ввода промокода\n'
        '🔗 <b>Подключиться</b> — поможет подключить приложение\n'
        '📱 <b>Подписка</b> — покажет состояние подписки\n'
        '🛠️ <b>Техподдержка</b> — свяжет с поддержкой\n\n'
        '🏠 <b>Кнопка "На главную"</b> включена по умолчанию, но вы можете отключить её '
        'при необходимости.{media_info}\n\n'
        'Выберите нужные кнопки и нажмите "Продолжить":',
    ).format(media_info='')

    keyboard = get_updated_message_buttons_selector_keyboard_with_media(selected_buttons, has_media, db_user.language)

    await message.answer(text, reply_markup=keyboard, parse_mode='HTML')


@admin_required
@error_handler
async def toggle_button_selection(callback: types.CallbackQuery, db_user: User, state: FSMContext):
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
    keyboard = get_updated_message_buttons_selector_keyboard_with_media(selected_buttons, has_media, db_user.language)

    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def confirm_button_selection(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    target = data.get('broadcast_target')
    message_text = data.get('broadcast_message')
    selected_buttons = data.get('selected_buttons')
    if selected_buttons is None:
        selected_buttons = list(DEFAULT_SELECTED_BUTTONS)
        await state.update_data(selected_buttons=selected_buttons)
    has_media = data.get('has_media', False)
    media_type = data.get('media_type')

    user_count = (
        await get_target_users_count(db, target)
        if not target.startswith('custom_')
        else await get_custom_users_count(db, target.replace('custom_', ''))
    )
    target_display = get_target_display_name(target, db_user.language)

    media_info = ''
    if has_media:
        media_type_names = {
            'photo': texts.t('ADMIN_MESSAGES_MEDIA_TYPE_PHOTO', 'Фотография'),
            'video': texts.t('ADMIN_MESSAGES_MEDIA_TYPE_VIDEO', 'Видео'),
            'document': texts.t('ADMIN_MESSAGES_MEDIA_TYPE_DOCUMENT', 'Документ'),
        }
        media_info = texts.t('ADMIN_MESSAGES_PREVIEW_MEDIA_INFO', '\n🖼️ <b>Медиафайл:</b> {media_type}').format(
            media_type=media_type_names.get(media_type, media_type)
        )

    ordered_keys = [button_key for row in BUTTON_ROWS for button_key in row]
    button_labels = get_broadcast_button_labels(db_user.language)
    selected_names = [button_labels[key] for key in ordered_keys if key in selected_buttons]
    if selected_names:
        buttons_info = texts.t('ADMIN_MESSAGES_PREVIEW_BUTTONS_SELECTED', '\n📘 <b>Кнопки:</b> {buttons}').format(
            buttons=', '.join(selected_names)
        )
    else:
        buttons_info = texts.t('ADMIN_MESSAGES_PREVIEW_BUTTONS_NONE', '\n📘 <b>Кнопки:</b> отсутствуют')

    preview_text = texts.t(
        'ADMIN_MESSAGES_PREVIEW_TEXT',
        '📨 <b>Предварительный просмотр рассылки</b>\n\n'
        '🎯 <b>Аудитория:</b> {target_display}\n'
        '👥 <b>Получателей:</b> {user_count}\n\n'
        '📝 <b>Сообщение:</b>\n'
        '{message_text}{media_info}\n\n'
        '{buttons_info}\n\n'
        'Подтвердить отправку?\n',
    ).format(
        target_display=target_display,
        user_count=user_count,
        message_text=message_text,
        media_info=media_info,
        buttons_info=buttons_info,
    )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_POLLS_SEND_CONFIRM_BUTTON', '✅ Отправить'),
                callback_data='admin_confirm_broadcast',
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_MESSAGES_PREVIEW_EDIT_BUTTONS_BUTTON', '📘 Изменить кнопки'),
                callback_data='edit_buttons',
            ),
        ]
    ]

    if has_media:
        keyboard.append(
            [types.InlineKeyboardButton(text=texts.t('ADMIN_BROADCAST_CHANGE_MEDIA', '🖼️ Изменить медиа'), callback_data='change_media')]
        )

    keyboard.append([types.InlineKeyboardButton(text=texts.t('ADMIN_CANCEL', '❌ Отмена'), callback_data='admin_messages')])

    # Если есть медиа, показываем его с загруженным фото, иначе обычное текстовое сообщение
    if has_media and media_type == 'photo':
        media_file_id = data.get('media_file_id')
        if media_file_id:
            # Удаляем текущее сообщение и отправляем новое с фото
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.bot.send_photo(
                chat_id=callback.message.chat.id,
                photo=media_file_id,
                caption=preview_text,
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
                parse_mode='HTML',
            )
        else:
            # Если нет file_id, используем safe редактирование
            await safe_edit_or_send_text(
                callback,
                preview_text,
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
                parse_mode='HTML',
            )
    else:
        # Для текстовых сообщений или других типов медиа используем safe редактирование
        await safe_edit_or_send_text(
            callback, preview_text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
        )

    await callback.answer()


@admin_required
@error_handler
async def confirm_broadcast(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
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

    # =========================================================================
    # КРИТИЧНО: Извлекаем ВСЕ скалярные значения из ORM-объектов СЕЙЧАС,
    # пока сессия активна. После начала рассылки соединение с БД может
    # закрыться по таймауту, и любое обращение к атрибутам ORM вызовет:
    # - MissingGreenlet (lazy loading вне async контекста)
    # - InterfaceError (соединение закрыто)
    # =========================================================================
    admin_id: int = db_user.id
    admin_name: str = db_user.full_name  # property, читает first_name/last_name
    admin_telegram_id: int | None = db_user.telegram_id
    admin_language: str = db_user.language

    await safe_edit_or_send_text(
        callback,
        texts.t(
            'ADMIN_MESSAGES_PREPARING',
            '📨 <b>Подготовка рассылки...</b>\n\n⏳ Загружаю список получателей...',
        ),
        reply_markup=None,
        parse_mode='HTML',
    )

    # Загружаем пользователей и сразу извлекаем telegram_id в список
    # чтобы не обращаться к ORM-объектам во время долгой рассылки
    if target.startswith('custom_'):
        users_orm = await get_custom_users(db, target.replace('custom_', ''))
    else:
        users_orm = await get_target_users(db, target)

    # Извлекаем только telegram_id - это всё что нужно для отправки
    # Фильтруем None (email-only пользователи)
    recipient_telegram_ids: list[int] = [user.telegram_id for user in users_orm if user.telegram_id is not None]
    total_users_count = len(users_orm)

    # Создаём запись истории рассылки
    broadcast_history = BroadcastHistory(
        target_type=target,
        message_text=message_text,
        has_media=has_media,
        media_type=media_type,
        media_file_id=media_file_id,
        media_caption=media_caption,
        total_count=total_users_count,
        sent_count=0,
        failed_count=0,
        admin_id=admin_id,
        admin_name=admin_name,
        status='in_progress',
    )
    db.add(broadcast_history)
    await db.commit()
    await db.refresh(broadcast_history)

    # Сохраняем ID - это единственное что нам нужно после коммита
    broadcast_id: int = broadcast_history.id

    # =========================================================================
    # С этого момента НЕ используем db сессию и ORM-объекты!
    # Работаем только со скалярными значениями.
    # =========================================================================

    sent_count = 0
    failed_count = 0

    broadcast_keyboard = create_broadcast_keyboard(selected_buttons, admin_language)

    # =========================================================================
    # Rate limiting: Telegram допускает ~30 msg/sec для бота.
    # Используем batch_size=25 + 1 сек задержка между батчами = ~25 msg/sec
    # с запасом, чтобы не получать FloodWait.
    # Semaphore=25 — все сообщения батча отправляются параллельно.
    # =========================================================================
    _BATCH_SIZE = 25
    _BATCH_DELAY = 1.0  # секунда между батчами
    _MAX_SEND_RETRIES = 3
    # Обновляем прогресс каждые N батчей (не каждое сообщение — иначе FloodWait на edit_text)
    _PROGRESS_UPDATE_INTERVAL = max(1, 500 // _BATCH_SIZE)  # ~каждые 500 сообщений
    # Минимальный интервал между обновлениями прогресса (секунды)
    _PROGRESS_MIN_INTERVAL = 5.0

    # Глобальная пауза при FloodWait — тормозим ВСЕ отправки, а не один слот семафора
    flood_wait_until: float = 0.0

    async def send_single_broadcast(telegram_id: int) -> bool:
        """Отправляет одно сообщение. Возвращает True при успехе."""
        nonlocal flood_wait_until

        for attempt in range(_MAX_SEND_RETRIES):
            # Глобальная пауза при FloodWait
            now = asyncio.get_event_loop().time()
            if flood_wait_until > now:
                await asyncio.sleep(flood_wait_until - now)

            try:
                if has_media and media_file_id:
                    send_method = {
                        'photo': callback.bot.send_photo,
                        'video': callback.bot.send_video,
                        'document': callback.bot.send_document,
                    }.get(media_type)
                    if send_method:
                        media_kwarg = {
                            'photo': 'photo',
                            'video': 'video',
                            'document': 'document',
                        }[media_type]
                        await send_method(
                            chat_id=telegram_id,
                            **{media_kwarg: media_file_id},
                            caption=message_text,
                            parse_mode='HTML',
                            reply_markup=broadcast_keyboard,
                        )
                    else:
                        # Неизвестный media_type — отправляем как текст
                        await callback.bot.send_message(
                            chat_id=telegram_id,
                            text=message_text,
                            parse_mode='HTML',
                            reply_markup=broadcast_keyboard,
                        )
                else:
                    await callback.bot.send_message(
                        chat_id=telegram_id,
                        text=message_text,
                        parse_mode='HTML',
                        reply_markup=broadcast_keyboard,
                    )
                return True

            except TelegramRetryAfter as e:
                # Глобальная пауза — тормозим все корутины
                wait_seconds = e.retry_after + 1
                flood_wait_until = asyncio.get_event_loop().time() + wait_seconds
                logger.warning(
                    'FloodWait: Telegram просит подождать сек (пользователь , попытка /)',
                    retry_after=e.retry_after,
                    telegram_id=telegram_id,
                    attempt=attempt + 1,
                    MAX_SEND_RETRIES=_MAX_SEND_RETRIES,
                )
                await asyncio.sleep(wait_seconds)

            except TelegramForbiddenError:
                return False

            except TelegramBadRequest as e:
                logger.debug('BadRequest при рассылке пользователю', telegram_id=telegram_id, e=e)
                return False

            except Exception as e:
                logger.error(
                    'Ошибка отправки пользователю (попытка /)',
                    telegram_id=telegram_id,
                    attempt=attempt + 1,
                    MAX_SEND_RETRIES=_MAX_SEND_RETRIES,
                    e=e,
                )
                if attempt < _MAX_SEND_RETRIES - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))

        return False

    # =========================================================================
    # Прогресс-бар в реальном времени (как в сканере заблокированных)
    # =========================================================================
    total_recipients = len(recipient_telegram_ids)
    last_progress_update: float = 0.0
    # ID сообщения, которое обновляем (может быть заменено при ошибке)
    progress_message = callback.message

    def _build_progress_text(
        current_sent: int,
        current_failed: int,
        total: int,
        phase: str = 'sending',
    ) -> str:
        processed = current_sent + current_failed
        percent = round(processed / total * 100, 1) if total > 0 else 0
        bar_length = 20
        filled = int(bar_length * processed / total) if total > 0 else 0
        bar = '█' * filled + '░' * (bar_length - filled)

        if phase == 'sending':
            return texts.t(
                'ADMIN_MESSAGES_PROGRESS_TEXT',
                '📨 <b>Рассылка в процессе...</b>\n\n'
                '[{bar}] {percent}%\n\n'
                '📊 <b>Прогресс:</b>\n'
                '• Отправлено: {current_sent}\n'
                '• Ошибок: {current_failed}\n'
                '• Обработано: {processed}/{total}\n\n'
                '⏳ Не закрывайте диалог — рассылка продолжается...',
            ).format(
                bar=bar,
                percent=percent,
                current_sent=current_sent,
                current_failed=current_failed,
                processed=processed,
                total=total,
            )
        return ''

    async def _update_progress_message(current_sent: int, current_failed: int) -> None:
        """Безопасно обновляет сообщение с прогрессом."""
        nonlocal last_progress_update, progress_message
        now = asyncio.get_event_loop().time()
        if now - last_progress_update < _PROGRESS_MIN_INTERVAL:
            return
        last_progress_update = now

        text = _build_progress_text(current_sent, current_failed, total_recipients)
        try:
            await progress_message.edit_text(text, parse_mode='HTML')
        except TelegramRetryAfter as e:
            # Не паникуем — пропускаем обновление прогресса
            logger.debug('FloodWait при обновлении прогресса, пропускаем: сек', retry_after=e.retry_after)
        except TelegramBadRequest:
            # Сообщение удалено или контент не изменился — отправляем новое
            try:
                progress_message = await callback.bot.send_message(
                    chat_id=callback.message.chat.id,
                    text=text,
                    parse_mode='HTML',
                )
            except Exception:
                pass
        except Exception:
            pass  # Не ломаем рассылку из-за ошибок обновления прогресса

    # Первое обновление прогресса
    await _update_progress_message(0, 0)

    # =========================================================================
    # Основной цикл рассылки — батчами по _BATCH_SIZE
    # =========================================================================
    for batch_idx, i in enumerate(range(0, total_recipients, _BATCH_SIZE)):
        batch = recipient_telegram_ids[i : i + _BATCH_SIZE]

        # Отправляем батч параллельно
        results = await asyncio.gather(
            *[send_single_broadcast(tid) for tid in batch],
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, bool):
                if result:
                    sent_count += 1
                else:
                    failed_count += 1
            elif isinstance(result, Exception):
                failed_count += 1
                logger.error('Необработанное исключение в рассылке', result=result)

        # Обновляем прогресс каждые _PROGRESS_UPDATE_INTERVAL батчей
        if batch_idx % _PROGRESS_UPDATE_INTERVAL == 0:
            await _update_progress_message(sent_count, failed_count)

        # Задержка между батчами для соблюдения rate limits
        await asyncio.sleep(_BATCH_DELAY)

    # Учитываем пропущенных email-only пользователей
    skipped_email_users = total_users_count - total_recipients
    if skipped_email_users > 0:
        logger.info('Пропущено email-only пользователей при рассылке', skipped_email_users=skipped_email_users)

    status = 'completed' if failed_count == 0 else 'partial'

    # Сохраняем результат в НОВОЙ сессии (старая уже мертва)
    await _persist_broadcast_result(
        broadcast_id=broadcast_id,
        sent_count=sent_count,
        failed_count=failed_count,
        status=status,
    )

    success_rate = round(sent_count / total_users_count * 100, 1) if total_users_count else 0
    media_info = (
        texts.t('ADMIN_MESSAGES_PREVIEW_MEDIA_INFO', '\n🖼️ <b>Медиафайл:</b> {media_type}').format(
            media_type=media_type
        )
        if has_media
        else ''
    )

    result_text = texts.t(
        'ADMIN_MESSAGES_RESULT_TEXT',
        '✅ <b>Рассылка завершена!</b>\n\n'
        '📊 <b>Результат:</b>\n'
        '• Отправлено: {sent_count}\n'
        '• Не доставлено: {failed_count}\n'
        '• Всего пользователей: {total_users_count}\n'
        '• Успешность: {success_rate}%{media_info}\n\n'
        '<b>Администратор:</b> {admin_name}',
    ).format(
        sent_count=sent_count,
        failed_count=failed_count,
        total_users_count=total_users_count,
        success_rate=success_rate,
        media_info=media_info,
        admin_name=admin_name,
    )

    back_keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_MESSAGES_BACK_BUTTON', '📨 К рассылкам'),
                    callback_data='admin_messages',
                )
            ]
        ]
    )

    try:
        await progress_message.edit_text(result_text, reply_markup=back_keyboard, parse_mode='HTML')
    except TelegramBadRequest as e:
        error_msg = str(e).lower()
        if (
            'message to edit not found' in error_msg
            or 'there is no text' in error_msg
            or "message can't be edited" in error_msg
        ):
            await callback.bot.send_message(
                chat_id=callback.message.chat.id,
                text=result_text,
                reply_markup=back_keyboard,
                parse_mode='HTML',
            )
        else:
            raise

    await state.clear()
    logger.info(
        'Рассылка завершена админом : sent failed total= (медиа:)',
        admin_telegram_id=admin_telegram_id,
        sent_count=sent_count,
        failed_count=failed_count,
        total_users_count=total_users_count,
        has_media=has_media,
    )


async def get_target_users_count(db: AsyncSession, target: str) -> int:
    """Быстрый подсчёт пользователей через SQL COUNT вместо загрузки всех в память."""
    from datetime import datetime, timedelta

    from sqlalchemy import distinct, func as sql_func

    base_filter = User.status == UserStatus.ACTIVE.value

    if target == 'all':
        query = select(sql_func.count(User.id)).where(base_filter)
        result = await db.execute(query)
        return result.scalar() or 0

    if target == 'active':
        # Активные платные подписки (не триал)
        query = (
            select(sql_func.count(distinct(User.id)))
            .join(Subscription, User.id == Subscription.user_id)
            .where(
                base_filter,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.is_trial == False,
            )
        )
        result = await db.execute(query)
        return result.scalar() or 0

    if target == 'trial':
        # Триальные подписки (без проверки is_active, как в оригинале)
        query = (
            select(sql_func.count(distinct(User.id)))
            .join(Subscription, User.id == Subscription.user_id)
            .where(
                base_filter,
                Subscription.is_trial == True,
            )
        )
        result = await db.execute(query)
        return result.scalar() or 0

    if target == 'no':
        # Без активной подписки - используем NOT EXISTS для корректности
        subquery = (
            select(Subscription.id)
            .where(
                Subscription.user_id == User.id,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
            )
            .exists()
        )
        query = select(sql_func.count(User.id)).where(base_filter, ~subquery)
        result = await db.execute(query)
        return result.scalar() or 0

    if target == 'expiring':
        # Истекающие в ближайшие 3 дня
        now = datetime.utcnow()
        expiry_threshold = now + timedelta(days=3)
        query = (
            select(sql_func.count(distinct(User.id)))
            .join(Subscription, User.id == Subscription.user_id)
            .where(
                base_filter,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.end_date <= expiry_threshold,
                Subscription.end_date > now,
            )
        )
        result = await db.execute(query)
        return result.scalar() or 0

    if target == 'expiring_subscribers':
        # Истекающие в ближайшие 7 дней
        now = datetime.utcnow()
        expiry_threshold = now + timedelta(days=7)
        query = (
            select(sql_func.count(distinct(User.id)))
            .join(Subscription, User.id == Subscription.user_id)
            .where(
                base_filter,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.end_date <= expiry_threshold,
                Subscription.end_date > now,
            )
        )
        result = await db.execute(query)
        return result.scalar() or 0

    if target == 'expired':
        # Истекшие подписки
        now = datetime.utcnow()
        expired_statuses = [SubscriptionStatus.EXPIRED.value, SubscriptionStatus.DISABLED.value]
        query = (
            select(sql_func.count(distinct(User.id)))
            .outerjoin(Subscription, User.id == Subscription.user_id)
            .where(
                base_filter,
                or_(
                    Subscription.status.in_(expired_statuses),
                    and_(Subscription.end_date <= now, Subscription.status != SubscriptionStatus.ACTIVE.value),
                    and_(Subscription.id == None, User.has_had_paid_subscription == True),
                ),
            )
        )
        result = await db.execute(query)
        return result.scalar() or 0

    if target == 'expired_subscribers':
        # То же что и expired
        now = datetime.utcnow()
        expired_statuses = [SubscriptionStatus.EXPIRED.value, SubscriptionStatus.DISABLED.value]
        query = (
            select(sql_func.count(distinct(User.id)))
            .outerjoin(Subscription, User.id == Subscription.user_id)
            .where(
                base_filter,
                or_(
                    Subscription.status.in_(expired_statuses),
                    and_(Subscription.end_date <= now, Subscription.status != SubscriptionStatus.ACTIVE.value),
                    and_(Subscription.id == None, User.has_had_paid_subscription == True),
                ),
            )
        )
        result = await db.execute(query)
        return result.scalar() or 0

    if target == 'active_zero':
        # Активные платные с нулевым трафиком
        query = (
            select(sql_func.count(distinct(User.id)))
            .join(Subscription, User.id == Subscription.user_id)
            .where(
                base_filter,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.is_trial == False,
                or_(Subscription.traffic_used_gb == None, Subscription.traffic_used_gb <= 0),
            )
        )
        result = await db.execute(query)
        return result.scalar() or 0

    if target == 'trial_zero':
        # Триальные с нулевым трафиком
        query = (
            select(sql_func.count(distinct(User.id)))
            .join(Subscription, User.id == Subscription.user_id)
            .where(
                base_filter,
                Subscription.is_trial == True,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                or_(Subscription.traffic_used_gb == None, Subscription.traffic_used_gb <= 0),
            )
        )
        result = await db.execute(query)
        return result.scalar() or 0

    if target == 'zero':
        # Все активные с нулевым трафиком
        query = (
            select(sql_func.count(distinct(User.id)))
            .join(Subscription, User.id == Subscription.user_id)
            .where(
                base_filter,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                or_(Subscription.traffic_used_gb == None, Subscription.traffic_used_gb <= 0),
            )
        )
        result = await db.execute(query)
        return result.scalar() or 0

    # Фильтр по тарифу
    if target.startswith('tariff_'):
        tariff_id = int(target.split('_')[1])
        query = (
            select(sql_func.count(distinct(User.id)))
            .join(Subscription, User.id == Subscription.user_id)
            .where(
                base_filter,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.tariff_id == tariff_id,
            )
        )
        result = await db.execute(query)
        return result.scalar() or 0

    # Custom filters — быстрый COUNT вместо загрузки всех пользователей
    if target.startswith('custom_'):
        now = datetime.utcnow()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        criteria = target[len('custom_') :]

        if criteria == 'today':
            query = select(sql_func.count(User.id)).where(base_filter, User.created_at >= today)
        elif criteria == 'week':
            query = select(sql_func.count(User.id)).where(base_filter, User.created_at >= now - timedelta(days=7))
        elif criteria == 'month':
            query = select(sql_func.count(User.id)).where(base_filter, User.created_at >= now - timedelta(days=30))
        elif criteria == 'active_today':
            query = select(sql_func.count(User.id)).where(base_filter, User.last_activity >= today)
        elif criteria == 'inactive_week':
            query = select(sql_func.count(User.id)).where(base_filter, User.last_activity < now - timedelta(days=7))
        elif criteria == 'inactive_month':
            query = select(sql_func.count(User.id)).where(base_filter, User.last_activity < now - timedelta(days=30))
        elif criteria == 'referrals':
            query = select(sql_func.count(User.id)).where(base_filter, User.referred_by_id.isnot(None))
        elif criteria == 'direct':
            query = select(sql_func.count(User.id)).where(base_filter, User.referred_by_id.is_(None))
        else:
            return 0

        result = await db.execute(query)
        return result.scalar() or 0

    return 0


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

    if target == 'all':
        return users

    if target == 'active':
        return [
            user
            for user in users
            if user.subscription and user.subscription.is_active and not user.subscription.is_trial
        ]

    if target == 'trial':
        return [user for user in users if user.subscription and user.subscription.is_trial]

    if target == 'no':
        return [user for user in users if not user.subscription or not user.subscription.is_active]

    if target == 'expiring':
        expiring_subs = await get_expiring_subscriptions(db, 3)
        return [sub.user for sub in expiring_subs if sub.user]

    if target == 'expired':
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

    if target == 'active_zero':
        return [
            user
            for user in users
            if user.subscription
            and not user.subscription.is_trial
            and user.subscription.is_active
            and (user.subscription.traffic_used_gb or 0) <= 0
        ]

    if target == 'trial_zero':
        return [
            user
            for user in users
            if user.subscription
            and user.subscription.is_trial
            and user.subscription.is_active
            and (user.subscription.traffic_used_gb or 0) <= 0
        ]

    if target == 'zero':
        return [
            user
            for user in users
            if user.subscription and user.subscription.is_active and (user.subscription.traffic_used_gb or 0) <= 0
        ]

    if target == 'expiring_subscribers':
        expiring_subs = await get_expiring_subscriptions(db, 7)
        return [sub.user for sub in expiring_subs if sub.user]

    if target == 'expired_subscribers':
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

    if target == 'canceled_subscribers':
        return [
            user
            for user in users
            if user.subscription and user.subscription.status == SubscriptionStatus.DISABLED.value
        ]

    if target == 'trial_ending':
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

    if target == 'trial_expired':
        now = datetime.utcnow()
        return [
            user
            for user in users
            if user.subscription and user.subscription.is_trial and user.subscription.end_date <= now
        ]

    if target == 'autopay_failed':
        from app.database.models import SubscriptionEvent

        week_ago = datetime.utcnow() - timedelta(days=7)
        stmt = (
            select(SubscriptionEvent.user_id)
            .where(
                and_(
                    SubscriptionEvent.event_type == 'autopay_failed',
                    SubscriptionEvent.occurred_at >= week_ago,
                )
            )
            .distinct()
        )
        result = await db.execute(stmt)
        failed_user_ids = set(result.scalars().all())
        return [user for user in users if user.id in failed_user_ids]

    if target == 'low_balance':
        threshold_kopeks = 10000  # 100 рублей
        return [
            user for user in users if (user.balance_kopeks or 0) < threshold_kopeks and (user.balance_kopeks or 0) > 0
        ]

    if target == 'inactive_30d':
        threshold = datetime.utcnow() - timedelta(days=30)
        return [user for user in users if user.last_activity and user.last_activity < threshold]

    if target == 'inactive_60d':
        threshold = datetime.utcnow() - timedelta(days=60)
        return [user for user in users if user.last_activity and user.last_activity < threshold]

    if target == 'inactive_90d':
        threshold = datetime.utcnow() - timedelta(days=90)
        return [user for user in users if user.last_activity and user.last_activity < threshold]

    # Фильтр по тарифу
    if target.startswith('tariff_'):
        tariff_id = int(target.split('_')[1])
        return [
            user
            for user in users
            if user.subscription and user.subscription.is_active and user.subscription.tariff_id == tariff_id
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

    if criteria == 'today':
        stmt = select(User).where(and_(User.status == 'active', User.created_at >= today))
    elif criteria == 'week':
        stmt = select(User).where(and_(User.status == 'active', User.created_at >= week_ago))
    elif criteria == 'month':
        stmt = select(User).where(and_(User.status == 'active', User.created_at >= month_ago))
    elif criteria == 'active_today':
        stmt = select(User).where(and_(User.status == 'active', User.last_activity >= today))
    elif criteria == 'inactive_week':
        stmt = select(User).where(and_(User.status == 'active', User.last_activity < week_ago))
    elif criteria == 'inactive_month':
        stmt = select(User).where(and_(User.status == 'active', User.last_activity < month_ago))
    elif criteria == 'referrals':
        stmt = select(User).where(and_(User.status == 'active', User.referred_by_id.isnot(None)))
    elif criteria == 'direct':
        stmt = select(User).where(and_(User.status == 'active', User.referred_by_id.is_(None)))
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

    stats['today'] = (
        await db.scalar(select(func.count(User.id)).where(and_(User.status == 'active', User.created_at >= today))) or 0
    )

    stats['week'] = (
        await db.scalar(select(func.count(User.id)).where(and_(User.status == 'active', User.created_at >= week_ago)))
        or 0
    )

    stats['month'] = (
        await db.scalar(select(func.count(User.id)).where(and_(User.status == 'active', User.created_at >= month_ago)))
        or 0
    )

    stats['active_today'] = (
        await db.scalar(select(func.count(User.id)).where(and_(User.status == 'active', User.last_activity >= today)))
        or 0
    )

    stats['inactive_week'] = (
        await db.scalar(select(func.count(User.id)).where(and_(User.status == 'active', User.last_activity < week_ago)))
        or 0
    )

    stats['inactive_month'] = (
        await db.scalar(
            select(func.count(User.id)).where(and_(User.status == 'active', User.last_activity < month_ago))
        )
        or 0
    )

    stats['referrals'] = (
        await db.scalar(
            select(func.count(User.id)).where(and_(User.status == 'active', User.referred_by_id.isnot(None)))
        )
        or 0
    )

    stats['direct'] = (
        await db.scalar(select(func.count(User.id)).where(and_(User.status == 'active', User.referred_by_id.is_(None))))
        or 0
    )

    return stats


def get_target_name(target_type: str, language: str = 'ru') -> str:
    texts = get_texts(language)
    names = {
        'all': texts.t('ADMIN_MESSAGES_TARGET_NAME_ALL', 'Всем пользователям'),
        'active': texts.t('ADMIN_MESSAGES_TARGET_NAME_ACTIVE', 'С активной подпиской'),
        'trial': texts.t('ADMIN_MESSAGES_TARGET_NAME_TRIAL', 'С триальной подпиской'),
        'no': texts.t('ADMIN_MESSAGES_TARGET_NAME_NO', 'Без подписки'),
        'sub': texts.t('ADMIN_MESSAGES_TARGET_NAME_NO', 'Без подписки'),
        'expiring': texts.t('ADMIN_MESSAGES_TARGET_NAME_EXPIRING', 'С истекающей подпиской'),
        'expired': texts.t('ADMIN_MESSAGES_TARGET_NAME_EXPIRED', 'С истекшей подпиской'),
        'active_zero': texts.t('ADMIN_MESSAGES_TARGET_NAME_ACTIVE_ZERO', 'Активная подписка, трафик 0 ГБ'),
        'trial_zero': texts.t('ADMIN_MESSAGES_TARGET_NAME_TRIAL_ZERO', 'Триальная подписка, трафик 0 ГБ'),
        'zero': texts.t('ADMIN_MESSAGES_TARGET_NAME_ZERO', 'Подписка, трафик 0 ГБ'),
        'custom_today': texts.t('ADMIN_MESSAGES_TARGET_NAME_CUSTOM_TODAY', 'Зарегистрированные сегодня'),
        'custom_week': texts.t('ADMIN_MESSAGES_TARGET_NAME_CUSTOM_WEEK', 'Зарегистрированные за неделю'),
        'custom_month': texts.t('ADMIN_MESSAGES_TARGET_NAME_CUSTOM_MONTH', 'Зарегистрированные за месяц'),
        'custom_active_today': texts.t('ADMIN_MESSAGES_TARGET_NAME_CUSTOM_ACTIVE_TODAY', 'Активные сегодня'),
        'custom_inactive_week': texts.t('ADMIN_MESSAGES_TARGET_NAME_CUSTOM_INACTIVE_WEEK', 'Неактивные 7+ дней'),
        'custom_inactive_month': texts.t('ADMIN_MESSAGES_TARGET_NAME_CUSTOM_INACTIVE_MONTH', 'Неактивные 30+ дней'),
        'custom_referrals': texts.t('ADMIN_MESSAGES_TARGET_NAME_CUSTOM_REFERRALS', 'Через рефералов'),
        'custom_direct': texts.t('ADMIN_MESSAGES_TARGET_NAME_CUSTOM_DIRECT', 'Прямая регистрация'),
    }
    # Обработка фильтра по тарифу
    if target_type.startswith('tariff_'):
        tariff_id = target_type.split('_')[1]
        return texts.t('ADMIN_MESSAGES_TARGET_NAME_TARIFF', 'По тарифу #{tariff_id}').format(tariff_id=tariff_id)
    return names.get(target_type, target_type)


def get_target_display_name(target: str, language: str = 'ru') -> str:
    return get_target_name(target, language)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_messages_menu, F.data == 'admin_messages')
    dp.callback_query.register(show_pinned_message_menu, F.data == 'admin_pinned_message')
    dp.callback_query.register(toggle_pinned_message_position, F.data == 'admin_pinned_message_position')
    dp.callback_query.register(toggle_pinned_message_start_mode, F.data == 'admin_pinned_message_start_mode')
    dp.callback_query.register(delete_pinned_message, F.data == 'admin_pinned_message_delete')
    dp.callback_query.register(prompt_pinned_message_update, F.data == 'admin_pinned_message_edit')
    dp.callback_query.register(handle_pinned_broadcast_now, F.data.startswith('admin_pinned_broadcast_now:'))
    dp.callback_query.register(handle_pinned_broadcast_skip, F.data.startswith('admin_pinned_broadcast_skip:'))
    dp.callback_query.register(show_broadcast_targets, F.data.in_(['admin_msg_all', 'admin_msg_by_sub']))
    dp.callback_query.register(show_tariff_filter, F.data == 'broadcast_by_tariff')
    dp.callback_query.register(select_broadcast_target, F.data.startswith('broadcast_'))
    dp.callback_query.register(confirm_broadcast, F.data == 'admin_confirm_broadcast')

    dp.callback_query.register(show_messages_history, F.data.startswith('admin_msg_history'))
    dp.callback_query.register(show_custom_broadcast, F.data == 'admin_msg_custom')
    dp.callback_query.register(select_custom_criteria, F.data.startswith('criteria_'))

    dp.callback_query.register(toggle_button_selection, F.data.startswith('btn_'))
    dp.callback_query.register(confirm_button_selection, F.data == 'buttons_confirm')
    dp.callback_query.register(show_button_selector_callback, F.data == 'edit_buttons')
    dp.callback_query.register(handle_media_selection, F.data.startswith('add_media_'))
    dp.callback_query.register(handle_media_selection, F.data == 'skip_media')
    dp.callback_query.register(handle_media_confirmation, F.data.in_(['confirm_media', 'replace_media']))
    dp.callback_query.register(handle_change_media, F.data == 'change_media')
    dp.message.register(process_broadcast_message, AdminStates.waiting_for_broadcast_message)
    dp.message.register(process_broadcast_media, AdminStates.waiting_for_broadcast_media)
    dp.message.register(process_pinned_message_update, AdminStates.editing_pinned_message)
