import html
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.promo_group import get_promo_group_by_id, get_promo_groups_with_counts
from app.database.crud.promocode import (
    create_promocode,
    delete_promocode,
    get_promocode_by_code,
    get_promocode_by_id,
    get_promocode_statistics,
    get_promocodes_count,
    get_promocodes_list,
    update_promocode,
)
from app.database.models import PromoCodeType, User
from app.keyboards.admin import (
    get_admin_pagination_keyboard,
    get_admin_promocodes_keyboard,
    get_promocode_type_keyboard,
)
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler
from app.utils.formatters import format_datetime


logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_promocodes_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    total_codes = await get_promocodes_count(db)
    active_codes = await get_promocodes_count(db, is_active=True)

    text = f"""
🎫 <b>Управление промокодами</b>

📊 <b>Статистика:</b>
- Всего промокодов: {total_codes}
- Активных: {active_codes}
- Неактивных: {total_codes - active_codes}

Выберите действие:
"""

    await callback.message.edit_text(text, reply_markup=get_admin_promocodes_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def show_promocodes_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession, page: int = 1):
    limit = 10
    offset = (page - 1) * limit

    promocodes = await get_promocodes_list(db, offset=offset, limit=limit)
    total_count = await get_promocodes_count(db)
    total_pages = (total_count + limit - 1) // limit

    if not promocodes:
        await callback.message.edit_text(
            '🎫 Промокоды не найдены',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_promocodes')]]
            ),
        )
        await callback.answer()
        return

    text = f'🎫 <b>Список промокодов</b> (стр. {page}/{total_pages})\n\n'
    keyboard = []

    for promo in promocodes:
        status_emoji = '✅' if promo.is_active else '❌'
        type_emoji = {
            'balance': '💰',
            'subscription_days': '📅',
            'trial_subscription': '🎁',
            'promo_group': '🏷️',
            'discount': '💸',
        }.get(promo.type, '🎫')

        text += f'{status_emoji} {type_emoji} <code>{promo.code}</code>\n'
        text += f'📊 Использований: {promo.current_uses}/{promo.max_uses}\n'

        if promo.type == PromoCodeType.BALANCE.value:
            text += f'💰 Бонус: {settings.format_price(promo.balance_bonus_kopeks)}\n'
        elif promo.type == PromoCodeType.SUBSCRIPTION_DAYS.value:
            text += f'📅 Дней: {promo.subscription_days}\n'
        elif promo.type == PromoCodeType.PROMO_GROUP.value:
            if promo.promo_group:
                text += f'🏷️ Промогруппа: {html.escape(promo.promo_group.name)}\n'
        elif promo.type == PromoCodeType.DISCOUNT.value:
            discount_hours = promo.subscription_days
            if discount_hours > 0:
                text += f'💸 Скидка: {promo.balance_bonus_kopeks}% ({discount_hours} ч.)\n'
            else:
                text += f'💸 Скидка: {promo.balance_bonus_kopeks}% (до покупки)\n'

        if promo.valid_until:
            text += f'⏰ До: {format_datetime(promo.valid_until)}\n'

        keyboard.append([types.InlineKeyboardButton(text=f'🎫 {promo.code}', callback_data=f'promo_manage_{promo.id}')])

        text += '\n'

    if total_pages > 1:
        pagination_row = get_admin_pagination_keyboard(
            page, total_pages, 'admin_promo_list', 'admin_promocodes', db_user.language
        ).inline_keyboard[0]
        keyboard.append(pagination_row)

    keyboard.extend(
        [
            [types.InlineKeyboardButton(text='➕ Создать', callback_data='admin_promo_create')],
            [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_promocodes')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_promocodes_list_page(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Обработчик пагинации списка промокодов."""
    try:
        page = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        page = 1
    await show_promocodes_list(callback, db_user, db, page=page)


@admin_required
@error_handler
async def show_promocode_management(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    promo_id = int(callback.data.split('_')[-1])

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer('❌ Промокод не найден', show_alert=True)
        return

    status_emoji = '✅' if promo.is_active else '❌'
    type_emoji = {
        'balance': '💰',
        'subscription_days': '📅',
        'trial_subscription': '🎁',
        'promo_group': '🏷️',
        'discount': '💸',
    }.get(promo.type, '🎫')

    text = f"""
🎫 <b>Управление промокодом</b>

{type_emoji} <b>Код:</b> <code>{promo.code}</code>
{status_emoji} <b>Статус:</b> {'Активен' if promo.is_active else 'Неактивен'}
📊 <b>Использований:</b> {promo.current_uses}/{promo.max_uses}
"""

    if promo.type == PromoCodeType.BALANCE.value:
        text += f'💰 <b>Бонус:</b> {settings.format_price(promo.balance_bonus_kopeks)}\n'
    elif promo.type == PromoCodeType.SUBSCRIPTION_DAYS.value:
        text += f'📅 <b>Дней:</b> {promo.subscription_days}\n'
    elif promo.type == PromoCodeType.PROMO_GROUP.value:
        if promo.promo_group:
            text += f'🏷️ <b>Промогруппа:</b> {html.escape(promo.promo_group.name)} (приоритет: {promo.promo_group.priority})\n'
        elif promo.promo_group_id:
            text += f'🏷️ <b>Промогруппа ID:</b> {promo.promo_group_id} (не найдена)\n'
    elif promo.type == PromoCodeType.DISCOUNT.value:
        discount_hours = promo.subscription_days
        if discount_hours > 0:
            text += f'💸 <b>Скидка:</b> {promo.balance_bonus_kopeks}% (срок: {discount_hours} ч.)\n'
        else:
            text += f'💸 <b>Скидка:</b> {promo.balance_bonus_kopeks}% (до первой покупки)\n'

    if promo.valid_until:
        text += f'⏰ <b>Действует до:</b> {format_datetime(promo.valid_until)}\n'

    first_purchase_only = getattr(promo, 'first_purchase_only', False)
    first_purchase_emoji = '✅' if first_purchase_only else '❌'
    text += f'🆕 <b>Только первая покупка:</b> {first_purchase_emoji}\n'

    text += f'📅 <b>Создан:</b> {format_datetime(promo.created_at)}\n'

    first_purchase_btn_text = '🆕 Первая покупка: ✅' if first_purchase_only else '🆕 Первая покупка: ❌'

    keyboard = [
        [
            types.InlineKeyboardButton(text='✏️ Редактировать', callback_data=f'promo_edit_{promo.id}'),
            types.InlineKeyboardButton(text='🔄 Переключить статус', callback_data=f'promo_toggle_{promo.id}'),
        ],
        [types.InlineKeyboardButton(text=first_purchase_btn_text, callback_data=f'promo_toggle_first_{promo.id}')],
        [
            types.InlineKeyboardButton(text='📊 Статистика', callback_data=f'promo_stats_{promo.id}'),
            types.InlineKeyboardButton(text='🗑️ Удалить', callback_data=f'promo_delete_{promo.id}'),
        ],
        [types.InlineKeyboardButton(text='⬅️ К списку', callback_data='admin_promo_list')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_promocode_edit_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer('❌ Ошибка получения ID промокода', show_alert=True)
        return

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer('❌ Промокод не найден', show_alert=True)
        return

    text = f"""
✏️ <b>Редактирование промокода</b> <code>{promo.code}</code>

💰 <b>Текущие параметры:</b>
"""

    if promo.type == PromoCodeType.BALANCE.value:
        text += f'• Бонус: {settings.format_price(promo.balance_bonus_kopeks)}\n'
    elif promo.type in [PromoCodeType.SUBSCRIPTION_DAYS.value, PromoCodeType.TRIAL_SUBSCRIPTION.value]:
        text += f'• Дней: {promo.subscription_days}\n'

    text += f'• Использований: {promo.current_uses}/{promo.max_uses}\n'

    if promo.valid_until:
        text += f'• До: {format_datetime(promo.valid_until)}\n'
    else:
        text += '• Срок: бессрочно\n'

    text += '\nВыберите параметр для изменения:'

    keyboard = [
        [types.InlineKeyboardButton(text='📅 Дата окончания', callback_data=f'promo_edit_date_{promo.id}')],
        [types.InlineKeyboardButton(text='📊 Количество использований', callback_data=f'promo_edit_uses_{promo.id}')],
    ]

    if promo.type == PromoCodeType.BALANCE.value:
        keyboard.insert(
            1, [types.InlineKeyboardButton(text='💰 Сумма бонуса', callback_data=f'promo_edit_amount_{promo.id}')]
        )
    elif promo.type in [PromoCodeType.SUBSCRIPTION_DAYS.value, PromoCodeType.TRIAL_SUBSCRIPTION.value]:
        keyboard.insert(
            1, [types.InlineKeyboardButton(text='📅 Количество дней', callback_data=f'promo_edit_days_{promo.id}')]
        )

    keyboard.extend([[types.InlineKeyboardButton(text='⬅️ Назад', callback_data=f'promo_manage_{promo.id}')]])

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def start_edit_promocode_date(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer('❌ Ошибка получения ID промокода', show_alert=True)
        return

    await state.update_data(editing_promo_id=promo_id, edit_action='date')

    text = f"""
📅 <b>Изменение даты окончания промокода</b>

Введите количество дней до окончания (от текущего момента):
• Введите <b>0</b> для бессрочного промокода
• Введите положительное число для установки срока

<i>Например: 30 (промокод будет действовать 30 дней)</i>

ID промокода: {promo_id}
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='❌ Отмена', callback_data=f'promo_edit_{promo_id}')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await state.set_state(AdminStates.setting_promocode_expiry)
    await callback.answer()


@admin_required
@error_handler
async def start_edit_promocode_amount(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer('❌ Ошибка получения ID промокода', show_alert=True)
        return

    await state.update_data(editing_promo_id=promo_id, edit_action='amount')

    text = f"""
💰 <b>Изменение суммы бонуса промокода</b>

Введите новую сумму в рублях:
<i>Например: 500</i>

ID промокода: {promo_id}
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='❌ Отмена', callback_data=f'promo_edit_{promo_id}')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await state.set_state(AdminStates.setting_promocode_value)
    await callback.answer()


@admin_required
@error_handler
async def start_edit_promocode_days(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    # ИСПРАВЛЕНИЕ: берем последний элемент как ID
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer('❌ Ошибка получения ID промокода', show_alert=True)
        return

    await state.update_data(editing_promo_id=promo_id, edit_action='days')

    text = f"""
📅 <b>Изменение количества дней подписки</b>

Введите новое количество дней:
<i>Например: 30</i>

ID промокода: {promo_id}
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='❌ Отмена', callback_data=f'promo_edit_{promo_id}')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await state.set_state(AdminStates.setting_promocode_value)
    await callback.answer()


@admin_required
@error_handler
async def start_edit_promocode_uses(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer('❌ Ошибка получения ID промокода', show_alert=True)
        return

    await state.update_data(editing_promo_id=promo_id, edit_action='uses')

    text = f"""
📊 <b>Изменение максимального количества использований</b>

Введите новое количество использований:
• Введите <b>0</b> для безлимитных использований
• Введите положительное число для ограничения

<i>Например: 100</i>

ID промокода: {promo_id}
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='❌ Отмена', callback_data=f'promo_edit_{promo_id}')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await state.set_state(AdminStates.setting_promocode_uses)
    await callback.answer()


@admin_required
@error_handler
async def start_promocode_creation(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    await callback.message.edit_text(
        '🎫 <b>Создание промокода</b>\n\nВыберите тип промокода:',
        reply_markup=get_promocode_type_keyboard(db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def select_promocode_type(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    promo_type = callback.data.split('_')[-1]

    type_names = {
        'balance': '💰 Пополнение баланса',
        'days': '📅 Дни подписки',
        'trial': '🎁 Тестовая подписка',
        'group': '🏷️ Промогруппа',
        'discount': '💸 Одноразовая скидка',
    }

    await state.update_data(promocode_type=promo_type)

    await callback.message.edit_text(
        f'🎫 <b>Создание промокода</b>\n\n'
        f'Тип: {type_names.get(promo_type, promo_type)}\n\n'
        f'Введите код промокода (только латинские буквы и цифры):',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='❌ Отмена', callback_data='admin_promocodes')]]
        ),
    )

    await state.set_state(AdminStates.creating_promocode)
    await callback.answer()


@admin_required
@error_handler
async def process_promocode_code(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    code = message.text.strip().upper()

    if not code.isalnum() or len(code) < 3 or len(code) > 20:
        await message.answer('❌ Код должен содержать только латинские буквы и цифры (3-20 символов)')
        return

    existing = await get_promocode_by_code(db, code)
    if existing:
        await message.answer('❌ Промокод с таким кодом уже существует')
        return

    await state.update_data(promocode_code=code)

    data = await state.get_data()
    promo_type = data.get('promocode_type')

    if promo_type == 'balance':
        await message.answer(f'💰 <b>Промокод:</b> <code>{code}</code>\n\nВведите сумму пополнения баланса (в рублях):')
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'days':
        await message.answer(f'📅 <b>Промокод:</b> <code>{code}</code>\n\nВведите количество дней подписки:')
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'trial':
        await message.answer(f'🎁 <b>Промокод:</b> <code>{code}</code>\n\nВведите количество дней тестовой подписки:')
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'discount':
        await message.answer(f'💸 <b>Промокод:</b> <code>{code}</code>\n\nВведите процент скидки (1-100):')
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'group':
        # Show promo group selection
        groups_with_counts = await get_promo_groups_with_counts(db, limit=50)

        if not groups_with_counts:
            await message.answer(
                '❌ Промогруппы не найдены. Создайте хотя бы одну промогруппу.',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_promocodes')]]
                ),
            )
            await state.clear()
            return

        keyboard = []
        text = f'🏷️ <b>Промокод:</b> <code>{code}</code>\n\nВыберите промогруппу для назначения:\n\n'

        for promo_group, user_count in groups_with_counts:
            text += (
                f'• {html.escape(promo_group.name)} (приоритет: {promo_group.priority}, пользователей: {user_count})\n'
            )
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=f'{promo_group.name} (↑{promo_group.priority})',
                        callback_data=f'promo_select_group_{promo_group.id}',
                    )
                ]
            )

        keyboard.append([types.InlineKeyboardButton(text='❌ Отмена', callback_data='admin_promocodes')])

        await message.answer(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
        await state.set_state(AdminStates.selecting_promo_group)


@admin_required
@error_handler
async def process_promo_group_selection(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession
):
    """Handle promo group selection for promocode"""
    try:
        promo_group_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer('❌ Ошибка получения ID промогруппы', show_alert=True)
        return

    promo_group = await get_promo_group_by_id(db, promo_group_id)
    if not promo_group:
        await callback.answer('❌ Промогруппа не найдена', show_alert=True)
        return

    await state.update_data(promo_group_id=promo_group_id, promo_group_name=promo_group.name)

    await callback.message.edit_text(
        f'🏷️ <b>Промокод для промогруппы</b>\n\n'
        f'Промогруппа: {html.escape(promo_group.name)}\n'
        f'Приоритет: {promo_group.priority}\n\n'
        f'📊 Введите количество использований промокода (или 0 для безлимита):'
    )

    await state.set_state(AdminStates.setting_promocode_uses)
    await callback.answer()


@admin_required
@error_handler
async def process_promocode_value(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()

    if data.get('editing_promo_id'):
        await handle_edit_value(message, db_user, state, db)
        return

    try:
        value = int(message.text.strip())

        promo_type = data.get('promocode_type')

        if promo_type == 'balance' and (value < 1 or value > 10000):
            await message.answer('❌ Сумма должна быть от 1 до 10,000 рублей')
            return
        if promo_type in ['days', 'trial'] and (value < 1 or value > 3650):
            await message.answer('❌ Количество дней должно быть от 1 до 3650')
            return
        if promo_type == 'discount' and (value < 1 or value > 100):
            await message.answer('❌ Процент скидки должен быть от 1 до 100')
            return

        await state.update_data(promocode_value=value)

        await message.answer('📊 Введите количество использований промокода (или 0 для безлимита):')
        await state.set_state(AdminStates.setting_promocode_uses)

    except ValueError:
        await message.answer('❌ Введите корректное число')


async def handle_edit_value(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()
    promo_id = data.get('editing_promo_id')
    edit_action = data.get('edit_action')

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await message.answer('❌ Промокод не найден')
        await state.clear()
        return

    try:
        value = int(message.text.strip())

        if edit_action == 'amount':
            if value < 1 or value > 10000:
                await message.answer('❌ Сумма должна быть от 1 до 10,000 рублей')
                return

            await update_promocode(db, promo, balance_bonus_kopeks=value * 100)
            await message.answer(
                f'✅ Сумма бонуса изменена на {value}₽',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [types.InlineKeyboardButton(text='🎫 К промокоду', callback_data=f'promo_manage_{promo_id}')]
                    ]
                ),
            )

        elif edit_action == 'days':
            if value < 1 or value > 3650:
                await message.answer('❌ Количество дней должно быть от 1 до 3650')
                return

            await update_promocode(db, promo, subscription_days=value)
            await message.answer(
                f'✅ Количество дней изменено на {value}',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [types.InlineKeyboardButton(text='🎫 К промокоду', callback_data=f'promo_manage_{promo_id}')]
                    ]
                ),
            )

        await state.clear()
        logger.info(
            'Промокод отредактирован администратором',
            code=promo.code,
            telegram_id=db_user.telegram_id,
            edit_action=edit_action,
            value=value,
        )

    except ValueError:
        await message.answer('❌ Введите корректное число')


@admin_required
@error_handler
async def process_promocode_uses(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()

    if data.get('editing_promo_id'):
        await handle_edit_uses(message, db_user, state, db)
        return

    try:
        max_uses = int(message.text.strip())

        if max_uses < 0 or max_uses > 100000:
            await message.answer('❌ Количество использований должно быть от 0 до 100,000')
            return

        if max_uses == 0:
            max_uses = 999999

        await state.update_data(promocode_max_uses=max_uses)

        await message.answer('⏰ Введите срок действия промокода в днях (или 0 для бессрочного):')
        await state.set_state(AdminStates.setting_promocode_expiry)

    except ValueError:
        await message.answer('❌ Введите корректное число')


async def handle_edit_uses(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()
    promo_id = data.get('editing_promo_id')

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await message.answer('❌ Промокод не найден')
        await state.clear()
        return

    try:
        max_uses = int(message.text.strip())

        if max_uses < 0 or max_uses > 100000:
            await message.answer('❌ Количество использований должно быть от 0 до 100,000')
            return

        if max_uses == 0:
            max_uses = 999999

        if max_uses < promo.current_uses:
            await message.answer(
                f'❌ Новый лимит ({max_uses}) не может быть меньше текущих использований ({promo.current_uses})'
            )
            return

        await update_promocode(db, promo, max_uses=max_uses)

        uses_text = 'безлимитное' if max_uses == 999999 else str(max_uses)
        await message.answer(
            f'✅ Максимальное количество использований изменено на {uses_text}',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🎫 К промокоду', callback_data=f'promo_manage_{promo_id}')]
                ]
            ),
        )

        await state.clear()
        logger.info(
            'Промокод отредактирован администратором max_uses',
            code=promo.code,
            telegram_id=db_user.telegram_id,
            max_uses=max_uses,
        )

    except ValueError:
        await message.answer('❌ Введите корректное число')


@admin_required
@error_handler
async def process_promocode_expiry(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()

    if data.get('editing_promo_id'):
        await handle_edit_expiry(message, db_user, state, db)
        return

    try:
        expiry_days = int(message.text.strip())

        if expiry_days < 0 or expiry_days > 3650:
            await message.answer('❌ Срок действия должен быть от 0 до 3650 дней')
            return

        code = data.get('promocode_code')
        promo_type = data.get('promocode_type')
        value = data.get('promocode_value', 0)
        max_uses = data.get('promocode_max_uses', 1)
        promo_group_id = data.get('promo_group_id')
        promo_group_name = data.get('promo_group_name')

        # Для DISCOUNT типа нужно дополнительно спросить срок действия скидки в часах
        if promo_type == 'discount':
            await state.update_data(promocode_expiry_days=expiry_days)
            await message.answer(
                f'⏰ <b>Промокод:</b> <code>{code}</code>\n\n'
                f'Введите срок действия скидки в часах (0-8760):\n'
                f'0 = бессрочно до первой покупки'
            )
            await state.set_state(AdminStates.setting_discount_hours)
            return

        valid_until = None
        if expiry_days > 0:
            valid_until = datetime.now(UTC) + timedelta(days=expiry_days)

        type_map = {
            'balance': PromoCodeType.BALANCE,
            'days': PromoCodeType.SUBSCRIPTION_DAYS,
            'trial': PromoCodeType.TRIAL_SUBSCRIPTION,
            'group': PromoCodeType.PROMO_GROUP,
        }

        promocode = await create_promocode(
            db=db,
            code=code,
            type=type_map[promo_type],
            balance_bonus_kopeks=value * 100 if promo_type == 'balance' else 0,
            subscription_days=value if promo_type in ['days', 'trial'] else 0,
            max_uses=max_uses,
            valid_until=valid_until,
            created_by=db_user.id,
            promo_group_id=promo_group_id if promo_type == 'group' else None,
        )

        type_names = {
            'balance': 'Пополнение баланса',
            'days': 'Дни подписки',
            'trial': 'Тестовая подписка',
            'group': 'Промогруппа',
        }

        summary_text = f"""
✅ <b>Промокод создан!</b>

🎫 <b>Код:</b> <code>{promocode.code}</code>
📝 <b>Тип:</b> {type_names.get(promo_type)}
"""

        if promo_type == 'balance':
            summary_text += f'💰 <b>Сумма:</b> {settings.format_price(promocode.balance_bonus_kopeks)}\n'
        elif promo_type in ['days', 'trial']:
            summary_text += f'📅 <b>Дней:</b> {promocode.subscription_days}\n'
        elif promo_type == 'group' and promo_group_name:
            summary_text += f'🏷️ <b>Промогруппа:</b> {promo_group_name}\n'

        summary_text += f'📊 <b>Использований:</b> {promocode.max_uses}\n'

        if promocode.valid_until:
            summary_text += f'⏰ <b>Действует до:</b> {format_datetime(promocode.valid_until)}\n'

        await message.answer(
            summary_text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='🎫 К промокодам', callback_data='admin_promocodes')]]
            ),
        )

        await state.clear()
        logger.info('Создан промокод администратором', code=code, telegram_id=db_user.telegram_id)

    except ValueError:
        await message.answer('❌ Введите корректное число дней')


@admin_required
@error_handler
async def process_discount_hours(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    """Обработчик ввода срока действия скидки в часах для DISCOUNT промокода."""
    data = await state.get_data()

    try:
        discount_hours = int(message.text.strip())

        if discount_hours < 0 or discount_hours > 8760:
            await message.answer('❌ Срок действия скидки должен быть от 0 до 8760 часов')
            return

        code = data.get('promocode_code')
        value = data.get('promocode_value', 0)  # Процент скидки
        max_uses = data.get('promocode_max_uses', 1)
        expiry_days = data.get('promocode_expiry_days', 0)

        valid_until = None
        if expiry_days > 0:
            valid_until = datetime.now(UTC) + timedelta(days=expiry_days)

        # Создаем DISCOUNT промокод
        # balance_bonus_kopeks = процент скидки (НЕ копейки!)
        # subscription_days = срок действия скидки в часах (НЕ дни!)
        promocode = await create_promocode(
            db=db,
            code=code,
            type=PromoCodeType.DISCOUNT,
            balance_bonus_kopeks=value,  # Процент (1-100)
            subscription_days=discount_hours,  # Часы (0-8760)
            max_uses=max_uses,
            valid_until=valid_until,
            created_by=db_user.id,
            promo_group_id=None,
        )

        summary_text = f"""
✅ <b>Промокод создан!</b>

🎫 <b>Код:</b> <code>{promocode.code}</code>
📝 <b>Тип:</b> Одноразовая скидка
💸 <b>Скидка:</b> {promocode.balance_bonus_kopeks}%
"""

        if discount_hours > 0:
            summary_text += f'⏰ <b>Срок скидки:</b> {discount_hours} ч.\n'
        else:
            summary_text += '⏰ <b>Срок скидки:</b> до первой покупки\n'

        summary_text += f'📊 <b>Использований:</b> {promocode.max_uses}\n'

        if promocode.valid_until:
            summary_text += f'⏳ <b>Промокод действует до:</b> {format_datetime(promocode.valid_until)}\n'

        await message.answer(
            summary_text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='🎫 К промокодам', callback_data='admin_promocodes')]]
            ),
        )

        await state.clear()
        logger.info(
            'Создан DISCOUNT промокод (%, ч) администратором',
            code=code,
            value=value,
            discount_hours=discount_hours,
            telegram_id=db_user.telegram_id,
        )

    except ValueError:
        await message.answer('❌ Введите корректное число часов')


async def handle_edit_expiry(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()
    promo_id = data.get('editing_promo_id')

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await message.answer('❌ Промокод не найден')
        await state.clear()
        return

    try:
        expiry_days = int(message.text.strip())

        if expiry_days < 0 or expiry_days > 3650:
            await message.answer('❌ Срок действия должен быть от 0 до 3650 дней')
            return

        valid_until = None
        if expiry_days > 0:
            valid_until = datetime.now(UTC) + timedelta(days=expiry_days)

        await update_promocode(db, promo, valid_until=valid_until)

        if valid_until:
            expiry_text = f'до {format_datetime(valid_until)}'
        else:
            expiry_text = 'бессрочно'

        await message.answer(
            f'✅ Срок действия промокода изменен: {expiry_text}',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🎫 К промокоду', callback_data=f'promo_manage_{promo_id}')]
                ]
            ),
        )

        await state.clear()
        logger.info(
            'Промокод отредактирован администратором expiry дней',
            code=promo.code,
            telegram_id=db_user.telegram_id,
            expiry_days=expiry_days,
        )

    except ValueError:
        await message.answer('❌ Введите корректное число дней')


@admin_required
@error_handler
async def toggle_promocode_status(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    promo_id = int(callback.data.split('_')[-1])

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer('❌ Промокод не найден', show_alert=True)
        return

    new_status = not promo.is_active
    await update_promocode(db, promo, is_active=new_status)

    status_text = 'активирован' if new_status else 'деактивирован'
    await callback.answer(f'✅ Промокод {status_text}', show_alert=True)

    await show_promocode_management(callback, db_user, db)


@admin_required
@error_handler
async def toggle_promocode_first_purchase(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Переключает режим 'только для первой покупки'."""
    promo_id = int(callback.data.split('_')[-1])

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer('❌ Промокод не найден', show_alert=True)
        return

    new_status = not getattr(promo, 'first_purchase_only', False)
    await update_promocode(db, promo, first_purchase_only=new_status)

    status_text = 'включён' if new_status else 'выключен'
    await callback.answer(f"✅ Режим 'первая покупка' {status_text}", show_alert=True)

    await show_promocode_management(callback, db_user, db)


@admin_required
@error_handler
async def confirm_delete_promocode(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer('❌ Ошибка получения ID промокода', show_alert=True)
        return

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer('❌ Промокод не найден', show_alert=True)
        return

    text = f"""
⚠️ <b>Подтверждение удаления</b>

Вы действительно хотите удалить промокод <code>{promo.code}</code>?

📊 <b>Информация о промокоде:</b>
• Использований: {promo.current_uses}/{promo.max_uses}
• Статус: {'Активен' if promo.is_active else 'Неактивен'}

<b>⚠️ Внимание:</b> Это действие нельзя отменить!

ID: {promo_id}
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text='✅ Да, удалить', callback_data=f'promo_delete_confirm_{promo.id}'),
                types.InlineKeyboardButton(text='❌ Отмена', callback_data=f'promo_manage_{promo.id}'),
            ]
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def delete_promocode_confirmed(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer('❌ Ошибка получения ID промокода', show_alert=True)
        return

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer('❌ Промокод не найден', show_alert=True)
        return

    code = promo.code
    success = await delete_promocode(db, promo)

    if success:
        await callback.answer(f'✅ Промокод {code} удален', show_alert=True)
        await show_promocodes_list(callback, db_user, db)
    else:
        await callback.answer('❌ Ошибка удаления промокода', show_alert=True)


@admin_required
@error_handler
async def show_promocode_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    promo_id = int(callback.data.split('_')[-1])

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer('❌ Промокод не найден', show_alert=True)
        return

    stats = await get_promocode_statistics(db, promo_id)

    text = f"""
📊 <b>Статистика промокода</b> <code>{promo.code}</code>

📈 <b>Общая статистика:</b>
- Всего использований: {stats['total_uses']}
- Использований сегодня: {stats['today_uses']}
- Осталось использований: {promo.max_uses - promo.current_uses}

📅 <b>Последние использования:</b>
"""

    if stats['recent_uses']:
        for use in stats['recent_uses'][:5]:
            use_date = format_datetime(use.used_at)

            if hasattr(use, 'user_username') and use.user_username:
                user_display = f'@{html.escape(use.user_username)}'
            elif hasattr(use, 'user_full_name') and use.user_full_name:
                user_display = html.escape(use.user_full_name)
            elif hasattr(use, 'user_telegram_id'):
                user_display = f'ID{use.user_telegram_id}'
            else:
                user_display = f'ID{use.user_id}'

            text += f'- {use_date} | {user_display}\n'
    else:
        text += '- Пока не было использований\n'

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ Назад', callback_data=f'promo_manage_{promo.id}')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def show_general_promocode_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    total_codes = await get_promocodes_count(db)
    active_codes = await get_promocodes_count(db, is_active=True)

    text = f"""
📊 <b>Общая статистика промокодов</b>

📈 <b>Основные показатели:</b>
- Всего промокодов: {total_codes}
- Активных: {active_codes}
- Неактивных: {total_codes - active_codes}

Для детальной статистики выберите конкретный промокод из списка.
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='🎫 К промокодам', callback_data='admin_promo_list')],
            [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_promocodes')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_promocodes_menu, F.data == 'admin_promocodes')
    dp.callback_query.register(show_promocodes_list, F.data == 'admin_promo_list')
    dp.callback_query.register(show_promocodes_list_page, F.data.startswith('admin_promo_list_page_'))
    dp.callback_query.register(start_promocode_creation, F.data == 'admin_promo_create')
    dp.callback_query.register(select_promocode_type, F.data.startswith('promo_type_'))
    dp.callback_query.register(process_promo_group_selection, F.data.startswith('promo_select_group_'))

    dp.callback_query.register(show_promocode_management, F.data.startswith('promo_manage_'))
    dp.callback_query.register(toggle_promocode_first_purchase, F.data.startswith('promo_toggle_first_'))
    dp.callback_query.register(toggle_promocode_status, F.data.startswith('promo_toggle_'))
    dp.callback_query.register(show_promocode_stats, F.data.startswith('promo_stats_'))

    dp.callback_query.register(start_edit_promocode_date, F.data.startswith('promo_edit_date_'))
    dp.callback_query.register(start_edit_promocode_amount, F.data.startswith('promo_edit_amount_'))
    dp.callback_query.register(start_edit_promocode_days, F.data.startswith('promo_edit_days_'))
    dp.callback_query.register(start_edit_promocode_uses, F.data.startswith('promo_edit_uses_'))
    dp.callback_query.register(show_general_promocode_stats, F.data == 'admin_promo_general_stats')

    dp.callback_query.register(show_promocode_edit_menu, F.data.regexp(r'^promo_edit_\d+$'))

    dp.callback_query.register(delete_promocode_confirmed, F.data.startswith('promo_delete_confirm_'))
    dp.callback_query.register(confirm_delete_promocode, F.data.startswith('promo_delete_'))

    dp.message.register(process_promocode_code, AdminStates.creating_promocode)
    dp.message.register(process_promocode_value, AdminStates.setting_promocode_value)
    dp.message.register(process_promocode_uses, AdminStates.setting_promocode_uses)
    dp.message.register(process_promocode_expiry, AdminStates.setting_promocode_expiry)
    dp.message.register(process_discount_hours, AdminStates.setting_discount_hours)
