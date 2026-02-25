import structlog
from aiogram import Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.rules import clear_all_rules, get_rules_statistics
from app.database.crud.ticket import TicketCRUD
from app.database.models import User
from app.handlers.admin import support_settings as support_settings_handlers
from app.keyboards.admin import (
    get_admin_communications_submenu_keyboard,
    get_admin_main_keyboard,
    get_admin_promo_submenu_keyboard,
    get_admin_settings_submenu_keyboard,
    get_admin_support_submenu_keyboard,
    get_admin_system_submenu_keyboard,
    get_admin_users_submenu_keyboard,
)
from app.localization.texts import clear_rules_cache, get_texts
from app.services.support_settings_service import SupportSettingsService
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_admin_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    admin_text = texts.ADMIN_PANEL
    try:
        from app.services.remnawave_service import RemnaWaveService

        remnawave_service = RemnaWaveService()
        stats = await remnawave_service.get_system_statistics()
        system_stats = stats.get('system', {})
        users_online = system_stats.get('users_online', 0)
        users_today = system_stats.get('users_last_day', 0)
        users_week = system_stats.get('users_last_week', 0)
        section_prompt = texts.t(
            'ADMIN_PANEL_SECTION_PROMPT',
            '\n\nВыберите раздел для управления:',
        )
        stats_block = texts.t(
            'ADMIN_PANEL_ONLINE_STATS_BLOCK',
            '\n\n- 🟢 Онлайн сейчас: {users_online}'
            '\n- 📅 Онлайн сегодня: {users_today}'
            '\n- 🗓️ На этой неделе: {users_week}',
        ).format(
            users_online=users_online,
            users_today=users_today,
            users_week=users_week,
        )
        if section_prompt in admin_text:
            admin_text = admin_text.replace(section_prompt, f'{stats_block}{section_prompt}')
        else:
            admin_text = f'{admin_text}{stats_block}'
    except Exception as e:
        logger.error('Не удалось получить статистику Remnawave для админ-панели', error=e)

    await callback.message.edit_text(admin_text, reply_markup=get_admin_main_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def show_users_submenu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('ADMIN_USERS_SUBMENU_TITLE', '👥 **Управление пользователями и подписками**\n\n')
        + texts.t('ADMIN_SUBMENU_SELECT_SECTION', 'Выберите нужный раздел:'),
        reply_markup=get_admin_users_submenu_keyboard(db_user.language),
        parse_mode='Markdown',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_promo_submenu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('ADMIN_PROMO_SUBMENU_TITLE', '💰 **Промокоды и статистика**\n\n')
        + texts.t('ADMIN_SUBMENU_SELECT_SECTION', 'Выберите нужный раздел:'),
        reply_markup=get_admin_promo_submenu_keyboard(db_user.language),
        parse_mode='Markdown',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_communications_submenu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('ADMIN_COMMUNICATIONS_SUBMENU_TITLE', '📨 **Коммуникации**\n\n')
        + texts.t('ADMIN_COMMUNICATIONS_SUBMENU_DESCRIPTION', 'Управление рассылками и текстами интерфейса:'),
        reply_markup=get_admin_communications_submenu_keyboard(db_user.language),
        parse_mode='Markdown',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_support_submenu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    # Moderators have access only to tickets and not to settings
    is_moderator_only = not settings.is_admin(callback.from_user.id) and SupportSettingsService.is_moderator(
        callback.from_user.id
    )

    kb = get_admin_support_submenu_keyboard(db_user.language)
    if is_moderator_only:
        # Rebuild keyboard to include only tickets and back to main menu
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_SUPPORT_TICKETS', '🎫 Тикеты поддержки'), callback_data='admin_tickets'
                    )
                ],
                [InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')],
            ]
        )
    await callback.message.edit_text(
        texts.t('ADMIN_SUPPORT_SUBMENU_TITLE', '🛟 **Поддержка**\n\n')
        + (
            texts.t('ADMIN_SUPPORT_SUBMENU_DESCRIPTION_MODERATOR', 'Доступ к тикетам.')
            if is_moderator_only
            else texts.t('ADMIN_SUPPORT_SUBMENU_DESCRIPTION', 'Управление тикетами и настройками поддержки:')
        ),
        reply_markup=kb,
        parse_mode='Markdown',
    )
    await callback.answer()


# Moderator panel entry (from main menu quick button)
async def show_moderator_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_SUPPORT_TICKETS', '🎫 Тикеты поддержки'), callback_data='admin_tickets'
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '⬅️ В главное меню'), callback_data='back_to_menu'
                )
            ],
        ]
    )
    await callback.message.edit_text(
        texts.t('ADMIN_SUPPORT_MODERATION_TITLE', '🧑‍⚖️ <b>Модерация поддержки</b>')
        + '\n\n'
        + texts.t('ADMIN_SUPPORT_MODERATION_DESCRIPTION', 'Доступ к тикетам поддержки.'),
        parse_mode='HTML',
        reply_markup=kb,
    )
    await callback.answer()


@admin_required
@error_handler
async def show_support_audit(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    # pagination
    page = 1
    if callback.data.startswith('admin_support_audit_page_'):
        try:
            page = int(callback.data.split('_')[-1])
        except Exception:
            page = 1
    per_page = 10
    total = await TicketCRUD.count_support_audit(db)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(page, 1)
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    logs = await TicketCRUD.list_support_audit(db, limit=per_page, offset=offset)

    lines = [texts.t('ADMIN_SUPPORT_AUDIT_TITLE', '🧾 <b>Аудит модераторов</b>'), '']
    if not logs:
        lines.append(texts.t('ADMIN_SUPPORT_AUDIT_EMPTY', 'Пока пусто'))
    else:
        for log in logs:
            role = (
                texts.t('ADMIN_SUPPORT_AUDIT_ROLE_MODERATOR', 'Модератор')
                if getattr(log, 'is_moderator', False)
                else texts.t('ADMIN_SUPPORT_AUDIT_ROLE_ADMIN', 'Админ')
            )
            ts = log.created_at.strftime('%d.%m.%Y %H:%M') if getattr(log, 'created_at', None) else ''
            action_map = {
                'close_ticket': texts.t('ADMIN_SUPPORT_AUDIT_ACTION_CLOSE_TICKET', 'Закрытие тикета'),
                'block_user_timed': texts.t('ADMIN_SUPPORT_AUDIT_ACTION_BLOCK_TIMED', 'Блокировка (время)'),
                'block_user_perm': texts.t('ADMIN_SUPPORT_AUDIT_ACTION_BLOCK_PERM', 'Блокировка (навсегда)'),
                'close_all_tickets': texts.t(
                    'ADMIN_SUPPORT_AUDIT_ACTION_CLOSE_ALL_TICKETS', 'Массовое закрытие тикетов'
                ),
                'unblock_user': texts.t('ADMIN_SUPPORT_AUDIT_ACTION_UNBLOCK', 'Снятие блока'),
            }
            action_text = action_map.get(log.action, log.action)
            ticket_part = (
                texts.t('ADMIN_SUPPORT_AUDIT_TICKET_PART', ' тикет #{ticket_id}').format(ticket_id=log.ticket_id)
                if log.ticket_id
                else ''
            )
            details = log.details or {}
            extra = ''
            if log.action == 'block_user_timed' and 'minutes' in details:
                extra = texts.t('ADMIN_SUPPORT_AUDIT_BLOCK_MINUTES_PART', ' ({minutes} мин)').format(
                    minutes=details['minutes']
                )
            elif log.action == 'close_all_tickets' and 'count' in details:
                extra = f' ({details["count"]})'
            actor_id_display = (
                log.actor_telegram_id or f'{texts.t("ADMIN_SUPPORT_AUDIT_ACTOR_USER_PREFIX", "user#")}{log.actor_user_id}'
                if log.actor_user_id
                else texts.t('ADMIN_SUPPORT_AUDIT_ACTOR_UNKNOWN', 'unknown')
            )
            lines.append(f'{ts} • {role} <code>{actor_id_display}</code> — {action_text}{ticket_part}{extra}')

    # keyboard with pagination
    nav_row = []
    if total_pages > 1:
        if page > 1:
            nav_row.append(InlineKeyboardButton(text='⬅️', callback_data=f'admin_support_audit_page_{page - 1}'))
        nav_row.append(InlineKeyboardButton(text=f'{page}/{total_pages}', callback_data='current_page'))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton(text='➡️', callback_data=f'admin_support_audit_page_{page + 1}'))

    kb_rows = []
    if nav_row:
        kb_rows.append(nav_row)
    kb_rows.append([InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_support')])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    await callback.message.edit_text('\n'.join(lines), parse_mode='HTML', reply_markup=kb)
    await callback.answer()


@admin_required
@error_handler
async def show_settings_submenu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('ADMIN_SETTINGS_SUBMENU_TITLE', '⚙️ **Настройки системы**\n\n')
        + texts.t('ADMIN_SETTINGS_SUBMENU_DESCRIPTION', 'Управление Remnawave, мониторингом и другими настройками:'),
        reply_markup=get_admin_settings_submenu_keyboard(db_user.language),
        parse_mode='Markdown',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_system_submenu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('ADMIN_SYSTEM_SUBMENU_TITLE', '🛠️ **Системные функции**\n\n')
        + texts.t(
            'ADMIN_SYSTEM_SUBMENU_DESCRIPTION', 'Отчеты, обновления, логи, резервные копии и системные операции:'
        ),
        reply_markup=get_admin_system_submenu_keyboard(db_user.language),
        parse_mode='Markdown',
    )
    await callback.answer()


@admin_required
@error_handler
async def clear_rules_command(message: types.Message, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    try:
        stats = await get_rules_statistics(db)

        if stats['total_active'] == 0:
            await message.reply(
                texts.t(
                    'ADMIN_RULES_ALREADY_CLEARED_MESSAGE',
                    'ℹ️ <b>Правила уже очищены</b>\n\n'
                    'В системе нет активных правил. Используются стандартные правила по умолчанию.',
                )
            )
            return

        success = await clear_all_rules(db, db_user.language)

        if success:
            clear_rules_cache()

            await message.reply(
                texts.t(
                    'ADMIN_RULES_CLEARED_SUCCESS_MESSAGE',
                    '✅ <b>Правила успешно очищены!</b>\n\n'
                    '📊 <b>Статистика:</b>\n'
                    '• Очищено правил: {total_active}\n'
                    '• Язык: {language}\n'
                    '• Выполнил: {full_name}\n\n'
                    'Теперь используются стандартные правила по умолчанию.',
                ).format(
                    total_active=stats['total_active'],
                    language=db_user.language,
                    full_name=db_user.full_name,
                )
            )

            logger.info(
                'Правила очищены командой администратором', telegram_id=db_user.telegram_id, full_name=db_user.full_name
            )
        else:
            await message.reply(
                texts.t(
                    'ADMIN_RULES_NOT_FOUND_MESSAGE',
                    '⚠️ <b>Нет правил для очистки</b>\n\nАктивные правила не найдены.',
                )
            )

    except Exception as e:
        logger.error('Ошибка при очистке правил командой', error=e)
        await message.reply(
            texts.t(
                'ADMIN_RULES_CLEAR_ERROR_MESSAGE',
                '❌ <b>Ошибка при очистке правил</b>\n\n'
                'Произошла ошибка: {error}\n'
                'Попробуйте через админ-панель или повторите позже.',
            ).format(error=e)
        )


@admin_required
@error_handler
async def rules_stats_command(message: types.Message, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    try:
        stats = await get_rules_statistics(db)

        if 'error' in stats:
            await message.reply(
                texts.t(
                    'ADMIN_RULES_STATS_FETCH_ERROR_MESSAGE',
                    '❌ Ошибка получения статистики: {error}',
                ).format(error=stats['error'])
            )
            return

        text = texts.t('ADMIN_RULES_STATS_HEADER', '📊 <b>Статистика правил сервиса</b>\n\n')
        text += texts.t('ADMIN_RULES_STATS_GENERAL_HEADER', '📋 <b>Общая информация:</b>\n')
        text += texts.t('ADMIN_RULES_STATS_ACTIVE_COUNT_LINE', '• Активных правил: {count}\n').format(
            count=stats['total_active']
        )
        text += texts.t('ADMIN_RULES_STATS_TOTAL_HISTORY_LINE', '• Всего в истории: {count}\n').format(
            count=stats['total_all_time']
        )
        text += texts.t('ADMIN_RULES_STATS_TOTAL_LANGUAGES_LINE', '• Поддерживаемых языков: {count}\n\n').format(
            count=stats['total_languages']
        )

        if stats['languages']:
            text += texts.t('ADMIN_RULES_STATS_BY_LANG_HEADER', '🌐 <b>По языкам:</b>\n')
            for lang, lang_stats in stats['languages'].items():
                text += texts.t(
                    'ADMIN_RULES_STATS_LANGUAGE_LINE',
                    '• <code>{lang}</code>: {active_count} правил, {content_length} символов\n',
                ).format(
                    lang=lang,
                    active_count=lang_stats['active_count'],
                    content_length=lang_stats['content_length'],
                )
                if lang_stats['last_updated']:
                    text += texts.t('ADMIN_RULES_STATS_UPDATED_LINE', '  Обновлено: {timestamp}\n').format(
                        timestamp=lang_stats['last_updated'].strftime('%d.%m.%Y %H:%M')
                    )
        else:
            text += texts.t(
                'ADMIN_RULES_STATS_EMPTY_MESSAGE',
                'ℹ️ Активных правил нет - используются правила по умолчанию',
            )

        await message.reply(text)

    except Exception as e:
        logger.error('Ошибка при получении статистики правил', error=e)
        await message.reply(
            texts.t(
                'ADMIN_RULES_STATS_ERROR_MESSAGE',
                '❌ <b>Ошибка получения статистики</b>\n\nПроизошла ошибка: {error}',
            ).format(error=e)
        )


@admin_required
@error_handler
async def admin_commands_help(message: types.Message, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    help_text = texts.t(
        'ADMIN_COMMANDS_HELP_TEXT',
        """
🔧 <b>Доступные админские команды:</b>

<b>📋 Управление правилами:</b>
• <code>/clear_rules</code> - очистить все правила
• <code>/rules_stats</code> - статистика правил

<b>ℹ️ Справка:</b>
• <code>/admin_help</code> - это сообщение

<b>📱 Панель управления:</b>
Используйте кнопку "Админ панель" в главном меню для полного доступа ко всем функциям.

<b>⚠️ Важно:</b>
Все команды логируются и требуют админских прав.
""",
    )

    await message.reply(help_text)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_admin_panel, F.data == 'admin_panel')

    dp.callback_query.register(show_users_submenu, F.data == 'admin_submenu_users')

    dp.callback_query.register(show_promo_submenu, F.data == 'admin_submenu_promo')

    dp.callback_query.register(show_communications_submenu, F.data == 'admin_submenu_communications')

    dp.callback_query.register(show_support_submenu, F.data == 'admin_submenu_support')
    dp.callback_query.register(
        show_support_audit, F.data.in_(['admin_support_audit']) | F.data.startswith('admin_support_audit_page_')
    )

    dp.callback_query.register(show_settings_submenu, F.data == 'admin_submenu_settings')

    dp.callback_query.register(show_system_submenu, F.data == 'admin_submenu_system')
    dp.callback_query.register(show_moderator_panel, F.data == 'moderator_panel')
    # Support settings module
    support_settings_handlers.register_handlers(dp)

    dp.message.register(clear_rules_command, Command('clear_rules'))

    dp.message.register(rules_stats_command, Command('rules_stats'))

    dp.message.register(admin_commands_help, Command('admin_help'))
