import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.bot_role import BOT_ROLE_SECTIONS, BotRoleCRUD
from app.database.crud.user import get_user_by_telegram_id
from app.database.models import User
from app.localization.texts import get_texts
from app.states import BotRoleStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)

SECTION_LABELS = {
    'users': '👥 Пользователи',
    'payments': '💳 Платежи',
    'tariffs': '📦 Тарифы',
    'subscriptions': '📋 Подписки',
    'promos': '🎟️ Промокоды',
    'broadcasts': '📨 Рассылки',
    'servers': '🌐 Серверы',
    'support': '🛟 Поддержка',
    'settings': '⚙️ Настройки',
    'analytics': '📊 Аналитика',
}


def _roles_list_keyboard(roles, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    rows = []
    for role in roles:
        user = role.user
        label = f'#{role.user_id}'
        if user:
            label = user.username or user.first_name or f'#{role.user_id}'
        perms_count = len(role.permissions or [])
        rows.append(
            [
                InlineKeyboardButton(
                    text=f'{label} ({perms_count} секций)',
                    callback_data=f'bot_role_view:{role.user_id}',
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text='➕ Добавить роль', callback_data='bot_role_add')]
    )
    rows.append(
        [InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _role_view_keyboard(user_id: int, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text='✏️ Изменить секции',
                    callback_data=f'bot_role_edit:{user_id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text='🗑️ Удалить роль',
                    callback_data=f'bot_role_delete:{user_id}',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_bot_roles')],
        ]
    )


def _permissions_keyboard(
    selected: list[str], user_id: int, language: str = 'ru'
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    rows = []
    for section in BOT_ROLE_SECTIONS:
        prefix = '✅' if section in selected else '⬜'
        label = SECTION_LABELS.get(section, section)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f'{prefix} {label}',
                    callback_data=f'bot_role_toggle:{user_id}:{section}',
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text='💾 Сохранить',
                callback_data=f'bot_role_save:{user_id}',
            ),
            InlineKeyboardButton(text=texts.BACK, callback_data='admin_bot_roles'),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@admin_required
@error_handler
async def admin_bot_roles(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    roles = await BotRoleCRUD.list_bot_roles(db)
    text = '👑 <b>Роли бота</b>\n\nНазначайте секции доступа для администраторов бота.'
    if not roles:
        text += '\n\nПока ролей нет.'
    await callback.message.edit_text(
        text,
        parse_mode='HTML',
        reply_markup=_roles_list_keyboard(roles, db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def bot_role_view(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split(':')[1])
    role = await BotRoleCRUD.get_bot_role(db, user_id)
    if not role:
        await callback.answer('Роль не найдена', show_alert=True)
        return

    user = role.user
    username = (user.username or user.first_name or str(user.telegram_id)) if user else f'#{user_id}'
    perms = role.permissions or []
    perms_text = '\n'.join(
        f'  {SECTION_LABELS.get(s, s)}' for s in perms
    ) or '  (нет секций)'

    text = (
        f'👑 <b>Роль для {username}</b>\n\n'
        f'<b>Секции доступа:</b>\n{perms_text}'
    )
    await callback.message.edit_text(
        text,
        parse_mode='HTML',
        reply_markup=_role_view_keyboard(user_id, db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def bot_role_add(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    await state.set_state(BotRoleStates.waiting_for_telegram_id)
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        '👑 <b>Добавление роли</b>\n\nВведите Telegram ID пользователя:',
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=texts.BACK, callback_data='admin_bot_roles')]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def bot_role_add_telegram_id(
    message: types.Message, db_user: User, state: FSMContext, db: AsyncSession
):
    text = message.text.strip()
    try:
        telegram_id = int(text)
    except ValueError:
        await message.answer('Введите числовой Telegram ID.')
        return

    user = await get_user_by_telegram_id(db, telegram_id)
    if not user:
        await message.answer(
            'Пользователь ещё не запускал бота. Попросите его открыть бота, затем выдайте роль.'
        )
        return

    existing = await BotRoleCRUD.get_bot_role(db, user.id)
    selected = list(existing.permissions or []) if existing else []

    await state.update_data(target_user_id=user.id, selected_permissions=selected)
    await state.set_state(BotRoleStates.selecting_permissions)
    await message.answer(
        f'Выберите секции доступа для <b>{user.username or user.first_name or telegram_id}</b>:',
        parse_mode='HTML',
        reply_markup=_permissions_keyboard(selected, user.id, db_user.language),
    )


@admin_required
@error_handler
async def bot_role_edit(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    user_id = int(callback.data.split(':')[1])
    role = await BotRoleCRUD.get_bot_role(db, user_id)
    selected = list(role.permissions or []) if role else []

    await state.update_data(target_user_id=user_id, selected_permissions=selected)
    await state.set_state(BotRoleStates.selecting_permissions)
    await callback.message.edit_text(
        '✏️ <b>Выберите секции доступа:</b>',
        parse_mode='HTML',
        reply_markup=_permissions_keyboard(selected, user_id, db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def bot_role_toggle(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    parts = callback.data.split(':')
    user_id = int(parts[1])
    section = parts[2]

    data = await state.get_data()
    if 'selected_permissions' not in data:
        await callback.answer('Сессия истекла, откройте роль заново.', show_alert=True)
        return
    selected = data.get('selected_permissions', [])

    if section in selected:
        selected.remove(section)
    else:
        selected.append(section)

    await state.update_data(selected_permissions=selected)
    await callback.message.edit_reply_markup(
        reply_markup=_permissions_keyboard(selected, user_id, db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def bot_role_save(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    parts = callback.data.split(':')
    user_id = int(parts[1])

    data = await state.get_data()
    if 'selected_permissions' not in data:
        await callback.answer('Сессия истекла, откройте роль заново.', show_alert=True)
        return

    selected = data.get('selected_permissions', [])
    if not selected:
        await callback.answer('Выберите хотя бы одну секцию.', show_alert=True)
        return

    await state.clear()

    await BotRoleCRUD.set_bot_role(db, user_id, selected, created_by=db_user.id)
    await db.commit()

    await callback.answer('Роль сохранена!', show_alert=True)

    # Refresh list
    roles = await BotRoleCRUD.list_bot_roles(db)
    await callback.message.edit_text(
        '👑 <b>Роли бота</b>\n\nРоль успешно сохранена.',
        parse_mode='HTML',
        reply_markup=_roles_list_keyboard(roles, db_user.language),
    )


@admin_required
@error_handler
async def bot_role_delete(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split(':')[1])
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        f'🗑️ Вы уверены, что хотите удалить роль для user #{user_id}?',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text='✅ Да, удалить',
                        callback_data=f'bot_role_delete_confirm:{user_id}',
                    ),
                    InlineKeyboardButton(text=texts.BACK, callback_data=f'bot_role_view:{user_id}'),
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def bot_role_delete_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split(':')[1])
    removed = await BotRoleCRUD.remove_bot_role(db, user_id)
    await db.commit()

    if removed:
        await callback.answer('Роль удалена!', show_alert=True)
    else:
        await callback.answer('Роль не найдена.', show_alert=True)

    roles = await BotRoleCRUD.list_bot_roles(db)
    await callback.message.edit_text(
        '👑 <b>Роли бота</b>\n\nНазначайте секции доступа для администраторов бота.',
        parse_mode='HTML',
        reply_markup=_roles_list_keyboard(roles, db_user.language),
    )


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(admin_bot_roles, F.data == 'admin_bot_roles')
    dp.callback_query.register(bot_role_view, F.data.startswith('bot_role_view:'))
    dp.callback_query.register(bot_role_add, F.data == 'bot_role_add')
    dp.callback_query.register(bot_role_edit, F.data.startswith('bot_role_edit:'))
    dp.callback_query.register(bot_role_toggle, F.data.startswith('bot_role_toggle:'))
    dp.callback_query.register(bot_role_save, F.data.startswith('bot_role_save:'))
    dp.callback_query.register(bot_role_delete, F.data.startswith('bot_role_delete:') & ~F.data.startswith('bot_role_delete_confirm:'))
    dp.callback_query.register(bot_role_delete_confirm, F.data.startswith('bot_role_delete_confirm:'))
    dp.message.register(bot_role_add_telegram_id, BotRoleStates.waiting_for_telegram_id)
