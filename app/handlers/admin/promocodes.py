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
from app.localization.texts import get_texts
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler
from app.utils.formatters import format_datetime


logger = structlog.get_logger(__name__)

PROMOCODE_TYPE_ICONS = {
    PromoCodeType.BALANCE.value: '💰',
    PromoCodeType.SUBSCRIPTION_DAYS.value: '📅',
    PromoCodeType.TRIAL_SUBSCRIPTION.value: '🎁',
    PromoCodeType.PROMO_GROUP.value: '🏷️',
    PromoCodeType.DISCOUNT.value: '💸',
}

PROMOCODE_TYPE_KEYS = {
    PromoCodeType.BALANCE.value: 'ADMIN_PROMOCODE_TYPE_BALANCE',
    PromoCodeType.SUBSCRIPTION_DAYS.value: 'ADMIN_PROMOCODE_TYPE_DAYS',
    PromoCodeType.TRIAL_SUBSCRIPTION.value: 'ADMIN_PROMOCODE_TYPE_TRIAL',
    PromoCodeType.PROMO_GROUP.value: 'ADMIN_PROMOCODE_TYPE_PROMO_GROUP',
    PromoCodeType.DISCOUNT.value: 'ADMIN_PROMOCODE_TYPE_DISCOUNT',
    'balance': 'ADMIN_PROMOCODE_TYPE_BALANCE',
    'days': 'ADMIN_PROMOCODE_TYPE_DAYS',
    'trial': 'ADMIN_PROMOCODE_TYPE_TRIAL',
    'group': 'ADMIN_PROMOCODE_TYPE_PROMO_GROUP',
    'discount': 'ADMIN_PROMOCODE_TYPE_DISCOUNT',
}


def _get_promocode_type_icon(promo_type: str) -> str:
    return PROMOCODE_TYPE_ICONS.get(promo_type, '🎫')


def _get_promocode_type_label(texts, promo_type: str) -> str:
    key = PROMOCODE_TYPE_KEYS.get(promo_type)
    if not key:
        return promo_type
    return texts.t(key, promo_type)


@admin_required
@error_handler
async def show_promocodes_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    total_codes = await get_promocodes_count(db)
    active_codes = await get_promocodes_count(db, is_active=True)
    text = texts.t(
        'ADMIN_PROMOCODES_MENU_TEXT',
        '🎫 <b>Управление промокодами</b>\n\n'
        '📊 <b>Статистика:</b>\n'
        '- Всего промокодов: {total_codes}\n'
        '- Активных: {active_codes}\n'
        '- Неактивных: {inactive_codes}\n\n'
        'Выберите действие:',
    ).format(total_codes=total_codes, active_codes=active_codes, inactive_codes=total_codes - active_codes)

    await callback.message.edit_text(text, reply_markup=get_admin_promocodes_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def show_promocodes_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession, page: int = 1):
    texts = get_texts(db_user.language)
    limit = 10
    offset = (page - 1) * limit

    promocodes = await get_promocodes_list(db, offset=offset, limit=limit)
    total_count = await get_promocodes_count(db)
    total_pages = (total_count + limit - 1) // limit

    if not promocodes:
        await callback.message.edit_text(
            texts.t('ADMIN_PROMOCODES_EMPTY', '🎫 Промокоды не найдены'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_promocodes')]]
            ),
        )
        await callback.answer()
        return

    text = (
        texts.t('ADMIN_PROMOCODES_LIST_TITLE', '🎫 <b>Список промокодов</b> (стр. {page}/{total_pages})')
        .format(page=page, total_pages=total_pages)
        + '\n\n'
    )
    keyboard = []

    for promo in promocodes:
        status_emoji = '✅' if promo.is_active else '❌'
        type_emoji = _get_promocode_type_icon(promo.type)

        text += f'{status_emoji} {type_emoji} <code>{promo.code}</code>\n'
        text += texts.t(
            'ADMIN_PROMOCODES_LIST_USES',
            '📊 Использований: {current_uses}/{max_uses}',
        ).format(current_uses=promo.current_uses, max_uses=promo.max_uses) + '\n'

        if promo.type == PromoCodeType.BALANCE.value:
            text += texts.t('ADMIN_PROMOCODES_LIST_BONUS', '💰 Бонус: {amount}').format(
                amount=settings.format_price(promo.balance_bonus_kopeks)
            ) + '\n'
        elif promo.type == PromoCodeType.SUBSCRIPTION_DAYS.value:
            text += texts.t('ADMIN_PROMOCODES_LIST_DAYS', '📅 Дней: {days}').format(days=promo.subscription_days) + '\n'
        elif promo.type == PromoCodeType.PROMO_GROUP.value:
            if promo.promo_group:
                text += texts.t('ADMIN_PROMOCODES_LIST_PROMO_GROUP', '🏷️ Промогруппа: {name}').format(
                    name=promo.promo_group.name
                ) + '\n'
        elif promo.type == PromoCodeType.DISCOUNT.value:
            discount_hours = promo.subscription_days
            if discount_hours > 0:
                text += texts.t('ADMIN_PROMOCODES_LIST_DISCOUNT_HOURS', '💸 Скидка: {percent}% ({hours} ч.)').format(
                    percent=promo.balance_bonus_kopeks, hours=discount_hours
                ) + '\n'
            else:
                text += texts.t(
                    'ADMIN_PROMOCODES_LIST_DISCOUNT_BEFORE_PURCHASE',
                    '💸 Скидка: {percent}% (до покупки)',
                ).format(percent=promo.balance_bonus_kopeks) + '\n'

        if promo.valid_until:
            text += texts.t('ADMIN_PROMOCODES_LIST_VALID_UNTIL', '⏰ До: {date}').format(
                date=format_datetime(promo.valid_until)
            ) + '\n'

        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMOCODES_LIST_ITEM_BUTTON', '🎫 {code}').format(code=promo.code),
                    callback_data=f'promo_manage_{promo.id}',
                )
            ]
        )

        text += '\n'

    if total_pages > 1:
        pagination_row = get_admin_pagination_keyboard(
            page, total_pages, 'admin_promo_list', 'admin_promocodes', db_user.language
        ).inline_keyboard[0]
        keyboard.append(pagination_row)

    keyboard.extend(
        [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMOCODES_CREATE', '➕ Создать'), callback_data='admin_promo_create'
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_promocodes')],
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
    texts = get_texts(db_user.language)
    promo_id = int(callback.data.split('_')[-1])

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer(texts.t('ADMIN_PROMOCODE_NOT_FOUND', '❌ Промокод не найден'), show_alert=True)
        return

    status_emoji = '✅' if promo.is_active else '❌'
    type_emoji = _get_promocode_type_icon(promo.type)
    status_text = (
        texts.t('ADMIN_PROMOCODE_STATUS_ACTIVE', 'Активен')
        if promo.is_active
        else texts.t('ADMIN_PROMOCODE_STATUS_INACTIVE', 'Неактивен')
    )

    lines = [
        texts.t('ADMIN_PROMOCODE_MANAGEMENT_TITLE', '🎫 <b>Управление промокодом</b>'),
        '',
        texts.t('ADMIN_PROMOCODE_MANAGEMENT_CODE', '{icon} <b>Код:</b> <code>{code}</code>').format(
            icon=type_emoji, code=promo.code
        ),
        texts.t('ADMIN_PROMOCODE_MANAGEMENT_STATUS', '{icon} <b>Статус:</b> {status}').format(
            icon=status_emoji, status=status_text
        ),
        texts.t(
            'ADMIN_PROMOCODE_MANAGEMENT_USES',
            '📊 <b>Использований:</b> {current_uses}/{max_uses}',
        ).format(current_uses=promo.current_uses, max_uses=promo.max_uses),
    ]

    if promo.type == PromoCodeType.BALANCE.value:
        lines.append(
            texts.t('ADMIN_PROMOCODE_MANAGEMENT_BONUS', '💰 <b>Бонус:</b> {amount}').format(
                amount=settings.format_price(promo.balance_bonus_kopeks)
            )
        )
    elif promo.type == PromoCodeType.SUBSCRIPTION_DAYS.value:
        lines.append(
            texts.t('ADMIN_PROMOCODE_MANAGEMENT_DAYS', '📅 <b>Дней:</b> {days}').format(days=promo.subscription_days)
        )
    elif promo.type == PromoCodeType.PROMO_GROUP.value:
        if promo.promo_group:
            lines.append(
                texts.t(
                    'ADMIN_PROMOCODE_MANAGEMENT_PROMO_GROUP',
                    '🏷️ <b>Промогруппа:</b> {name} (приоритет: {priority})',
                ).format(name=promo.promo_group.name, priority=promo.promo_group.priority)
            )
        elif promo.promo_group_id:
            lines.append(
                texts.t(
                    'ADMIN_PROMOCODE_MANAGEMENT_PROMO_GROUP_ID',
                    '🏷️ <b>Промогруппа ID:</b> {promo_group_id} (не найдена)',
                ).format(promo_group_id=promo.promo_group_id)
            )
    elif promo.type == PromoCodeType.DISCOUNT.value:
        discount_hours = promo.subscription_days
        if discount_hours > 0:
            lines.append(
                texts.t(
                    'ADMIN_PROMOCODE_MANAGEMENT_DISCOUNT_HOURS',
                    '💸 <b>Скидка:</b> {percent}% (срок: {hours} ч.)',
                ).format(percent=promo.balance_bonus_kopeks, hours=discount_hours)
            )
        else:
            lines.append(
                texts.t(
                    'ADMIN_PROMOCODE_MANAGEMENT_DISCOUNT_BEFORE_FIRST_PURCHASE',
                    '💸 <b>Скидка:</b> {percent}% (до первой покупки)',
                ).format(percent=promo.balance_bonus_kopeks)
            )

    if promo.valid_until:
        lines.append(
            texts.t('ADMIN_PROMOCODE_MANAGEMENT_VALID_UNTIL', '⏰ <b>Действует до:</b> {date}').format(
                date=format_datetime(promo.valid_until)
            )
        )

    first_purchase_only = getattr(promo, 'first_purchase_only', False)
    first_purchase_emoji = '✅' if first_purchase_only else '❌'
    lines.append(
        texts.t('ADMIN_PROMOCODE_MANAGEMENT_FIRST_PURCHASE', '🆕 <b>Только первая покупка:</b> {status}').format(
            status=first_purchase_emoji
        )
    )

    lines.append(
        texts.t('ADMIN_PROMOCODE_MANAGEMENT_CREATED_AT', '📅 <b>Создан:</b> {date}').format(
            date=format_datetime(promo.created_at)
        )
    )

    text = '\n'.join(lines)

    first_purchase_btn_text = (
        texts.t('ADMIN_PROMOCODE_FIRST_PURCHASE_ENABLED', '🆕 Первая покупка: ✅')
        if first_purchase_only
        else texts.t('ADMIN_PROMOCODE_FIRST_PURCHASE_DISABLED', '🆕 Первая покупка: ❌')
    )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_PROMOCODE_EDIT', '✏️ Редактировать'), callback_data=f'promo_edit_{promo.id}'
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_PROMOCODE_TOGGLE_BUTTON', '🔄 Переключить статус'),
                callback_data=f'promo_toggle_{promo.id}',
            ),
        ],
        [types.InlineKeyboardButton(text=first_purchase_btn_text, callback_data=f'promo_toggle_first_{promo.id}')],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_PROMOCODE_STATS', '📊 Статистика'), callback_data=f'promo_stats_{promo.id}'
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_PROMOCODE_DELETE', '🗑️ Удалить'), callback_data=f'promo_delete_{promo.id}'
            ),
        ],
        [types.InlineKeyboardButton(text=texts.t('ADMIN_BACK_TO_LIST', '⬅️ К списку'), callback_data='admin_promo_list')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_promocode_edit_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_PROMOCODE_ID_PARSE_ERROR', '❌ Ошибка получения ID промокода'), show_alert=True)
        return

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer(texts.t('ADMIN_PROMOCODE_NOT_FOUND', '❌ Промокод не найден'), show_alert=True)
        return

    text = (
        texts.t('ADMIN_PROMOCODE_EDIT_MENU_TITLE', '✏️ <b>Редактирование промокода</b> <code>{code}</code>').format(
            code=promo.code
        )
        + '\n\n'
        + texts.t('ADMIN_PROMOCODE_EDIT_MENU_CURRENT_PARAMS', '💰 <b>Текущие параметры:</b>')
        + '\n'
    )

    if promo.type == PromoCodeType.BALANCE.value:
        text += texts.t('ADMIN_PROMOCODE_EDIT_MENU_BONUS', '• Бонус: {amount}').format(
            amount=settings.format_price(promo.balance_bonus_kopeks)
        ) + '\n'
    elif promo.type in [PromoCodeType.SUBSCRIPTION_DAYS.value, PromoCodeType.TRIAL_SUBSCRIPTION.value]:
        text += texts.t('ADMIN_PROMOCODE_EDIT_MENU_DAYS', '• Дней: {days}').format(days=promo.subscription_days) + '\n'

    text += texts.t('ADMIN_PROMOCODE_EDIT_MENU_USES', '• Использований: {current_uses}/{max_uses}').format(
        current_uses=promo.current_uses, max_uses=promo.max_uses
    ) + '\n'

    if promo.valid_until:
        text += texts.t('ADMIN_PROMOCODE_EDIT_MENU_VALID_UNTIL', '• До: {date}').format(
            date=format_datetime(promo.valid_until)
        ) + '\n'
    else:
        text += texts.t('ADMIN_PROMOCODE_EDIT_MENU_UNLIMITED', '• Срок: бессрочно') + '\n'

    text += '\n' + texts.t('ADMIN_PROMO_GROUP_EDIT_MENU_HINT', 'Выберите параметр для изменения:')

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_PROMOCODE_EDIT_BUTTON_EXPIRY', '📅 Дата окончания'),
                callback_data=f'promo_edit_date_{promo.id}',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_PROMOCODE_EDIT_BUTTON_USES', '📊 Количество использований'),
                callback_data=f'promo_edit_uses_{promo.id}',
            )
        ],
    ]

    if promo.type == PromoCodeType.BALANCE.value:
        keyboard.insert(
            1,
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMOCODE_EDIT_BUTTON_AMOUNT', '💰 Сумма бонуса'),
                    callback_data=f'promo_edit_amount_{promo.id}',
                )
            ],
        )
    elif promo.type in [PromoCodeType.SUBSCRIPTION_DAYS.value, PromoCodeType.TRIAL_SUBSCRIPTION.value]:
        keyboard.insert(
            1,
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMOCODE_EDIT_BUTTON_DAYS', '📅 Количество дней'),
                    callback_data=f'promo_edit_days_{promo.id}',
                )
            ],
        )

    keyboard.extend([[types.InlineKeyboardButton(text=texts.BACK, callback_data=f'promo_manage_{promo.id}')]])

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def start_edit_promocode_date(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_PROMOCODE_ID_PARSE_ERROR', '❌ Ошибка получения ID промокода'), show_alert=True)
        return

    await state.update_data(editing_promo_id=promo_id, edit_action='date')

    text = texts.t(
        'ADMIN_PROMOCODE_EDIT_EXPIRY_PROMPT',
        '📅 <b>Изменение даты окончания промокода</b>\n\n'
        'Введите количество дней до окончания (от текущего момента):\n'
        '• Введите <b>0</b> для бессрочного промокода\n'
        '• Введите положительное число для установки срока\n\n'
        '<i>Например: 30 (промокод будет действовать 30 дней)</i>\n\n'
        'ID промокода: {promo_id}',
    ).format(promo_id=promo_id)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'promo_edit_{promo_id}')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await state.set_state(AdminStates.setting_promocode_expiry)
    await callback.answer()


@admin_required
@error_handler
async def start_edit_promocode_amount(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_PROMOCODE_ID_PARSE_ERROR', '❌ Ошибка получения ID промокода'), show_alert=True)
        return

    await state.update_data(editing_promo_id=promo_id, edit_action='amount')

    text = texts.t(
        'ADMIN_PROMOCODE_EDIT_AMOUNT_PROMPT',
        '💰 <b>Изменение суммы бонуса промокода</b>\n\n'
        'Введите новую сумму в рублях:\n'
        '<i>Например: 500</i>\n\n'
        'ID промокода: {promo_id}',
    ).format(promo_id=promo_id)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'promo_edit_{promo_id}')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await state.set_state(AdminStates.setting_promocode_value)
    await callback.answer()


@admin_required
@error_handler
async def start_edit_promocode_days(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    # ИСПРАВЛЕНИЕ: берем последний элемент как ID
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_PROMOCODE_ID_PARSE_ERROR', '❌ Ошибка получения ID промокода'), show_alert=True)
        return

    await state.update_data(editing_promo_id=promo_id, edit_action='days')

    text = texts.t(
        'ADMIN_PROMOCODE_EDIT_DAYS_PROMPT',
        '📅 <b>Изменение количества дней подписки</b>\n\n'
        'Введите новое количество дней:\n'
        '<i>Например: 30</i>\n\n'
        'ID промокода: {promo_id}',
    ).format(promo_id=promo_id)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'promo_edit_{promo_id}')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await state.set_state(AdminStates.setting_promocode_value)
    await callback.answer()


@admin_required
@error_handler
async def start_edit_promocode_uses(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_PROMOCODE_ID_PARSE_ERROR', '❌ Ошибка получения ID промокода'), show_alert=True)
        return

    await state.update_data(editing_promo_id=promo_id, edit_action='uses')

    text = texts.t(
        'ADMIN_PROMOCODE_EDIT_USES_PROMPT',
        '📊 <b>Изменение максимального количества использований</b>\n\n'
        'Введите новое количество использований:\n'
        '• Введите <b>0</b> для безлимитных использований\n'
        '• Введите положительное число для ограничения\n\n'
        '<i>Например: 100</i>\n\n'
        'ID промокода: {promo_id}',
    ).format(promo_id=promo_id)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'promo_edit_{promo_id}')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await state.set_state(AdminStates.setting_promocode_uses)
    await callback.answer()


@admin_required
@error_handler
async def start_promocode_creation(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t('ADMIN_PROMOCODE_CREATE_START', '🎫 <b>Создание промокода</b>\n\nВыберите тип промокода:'),
        reply_markup=get_promocode_type_keyboard(db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def select_promocode_type(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    promo_type = callback.data.split('_')[-1]

    await state.update_data(promocode_type=promo_type)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_PROMOCODE_CREATE_CODE_PROMPT',
            '🎫 <b>Создание промокода</b>\n\nТип: {type_label}\n\n'
            'Введите код промокода (только латинские буквы и цифры):',
        ).format(type_label=_get_promocode_type_label(texts, promo_type)),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_promocodes')]]
        ),
    )

    await state.set_state(AdminStates.creating_promocode)
    await callback.answer()


@admin_required
@error_handler
async def process_promocode_code(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    code = message.text.strip().upper()

    if not code.isalnum() or len(code) < 3 or len(code) > 20:
        await message.answer(
            texts.t(
                'ADMIN_PROMOCODE_CODE_INVALID',
                '❌ Код должен содержать только латинские буквы и цифры (3-20 символов)',
            )
        )
        return

    existing = await get_promocode_by_code(db, code)
    if existing:
        await message.answer(texts.t('ADMIN_PROMOCODE_CODE_EXISTS', '❌ Промокод с таким кодом уже существует'))
        return

    await state.update_data(promocode_code=code)

    data = await state.get_data()
    promo_type = data.get('promocode_type')

    if promo_type == 'balance':
        await message.answer(
            texts.t(
                'ADMIN_PROMOCODE_INPUT_BALANCE',
                '💰 <b>Промокод:</b> <code>{code}</code>\n\nВведите сумму пополнения баланса (в рублях):',
            ).format(code=code)
        )
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'days':
        await message.answer(
            texts.t(
                'ADMIN_PROMOCODE_INPUT_DAYS',
                '📅 <b>Промокод:</b> <code>{code}</code>\n\nВведите количество дней подписки:',
            ).format(code=code)
        )
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'trial':
        await message.answer(
            texts.t(
                'ADMIN_PROMOCODE_INPUT_TRIAL_DAYS',
                '🎁 <b>Промокод:</b> <code>{code}</code>\n\nВведите количество дней тестовой подписки:',
            ).format(code=code)
        )
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'discount':
        await message.answer(
            texts.t(
                'ADMIN_PROMOCODE_INPUT_DISCOUNT_PERCENT',
                '💸 <b>Промокод:</b> <code>{code}</code>\n\nВведите процент скидки (1-100):',
            ).format(code=code)
        )
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'group':
        # Show promo group selection
        groups_with_counts = await get_promo_groups_with_counts(db, limit=50)

        if not groups_with_counts:
            await message.answer(
                texts.t('ADMIN_PROMOCODE_PROMO_GROUPS_EMPTY', '❌ Промогруппы не найдены. Создайте хотя бы одну промогруппу.'),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_promocodes')]]
                ),
            )
            await state.clear()
            return

        keyboard = []
        text = (
            texts.t(
                'ADMIN_PROMOCODE_SELECT_PROMO_GROUP',
                '🏷️ <b>Промокод:</b> <code>{code}</code>\n\nВыберите промогруппу для назначения:',
            ).format(code=code)
            + '\n\n'
        )

        for promo_group, user_count in groups_with_counts:
            text += texts.t(
                'ADMIN_PROMOCODE_SELECT_PROMO_GROUP_LINE',
                '• {name} (приоритет: {priority}, пользователей: {users})',
            ).format(name=promo_group.name, priority=promo_group.priority, users=user_count) + '\n'
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_PROMOCODE_SELECT_PROMO_GROUP_BUTTON', '{name} (↑{priority})').format(
                            name=promo_group.name, priority=promo_group.priority
                        ),
                        callback_data=f'promo_select_group_{promo_group.id}',
                    )
                ]
            )

        keyboard.append([types.InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_promocodes')])

        await message.answer(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
        await state.set_state(AdminStates.selecting_promo_group)


@admin_required
@error_handler
async def process_promo_group_selection(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession
):
    """Handle promo group selection for promocode"""
    texts = get_texts(db_user.language)
    try:
        promo_group_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer(
            texts.t('ADMIN_PROMOCODE_PROMO_GROUP_ID_PARSE_ERROR', '❌ Ошибка получения ID промогруппы'),
            show_alert=True,
        )
        return

    promo_group = await get_promo_group_by_id(db, promo_group_id)
    if not promo_group:
        await callback.answer(texts.t('ADMIN_PROMO_GROUP_NOT_FOUND', '❌ Промогруппа не найдена'), show_alert=True)
        return

    await state.update_data(promo_group_id=promo_group_id, promo_group_name=promo_group.name)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_PROMOCODE_PROMO_GROUP_SELECTED',
            '🏷️ <b>Промокод для промогруппы</b>\n\n'
            'Промогруппа: {name}\n'
            'Приоритет: {priority}\n\n'
            '📊 Введите количество использований промокода (или 0 для безлимита):',
        ).format(name=promo_group.name, priority=promo_group.priority)
    )

    await state.set_state(AdminStates.setting_promocode_uses)
    await callback.answer()


@admin_required
@error_handler
async def process_promocode_value(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()

    if data.get('editing_promo_id'):
        await handle_edit_value(message, db_user, state, db)
        return

    try:
        value = int(message.text.strip())

        promo_type = data.get('promocode_type')

        if promo_type == 'balance' and (value < 1 or value > 10000):
            await message.answer(texts.t('ADMIN_PROMOCODE_AMOUNT_RANGE_ERROR', '❌ Сумма должна быть от 1 до 10,000 рублей'))
            return
        if promo_type in ['days', 'trial'] and (value < 1 or value > 3650):
            await message.answer(texts.t('ADMIN_PROMOCODE_DAYS_RANGE_ERROR', '❌ Количество дней должно быть от 1 до 3650'))
            return
        if promo_type == 'discount' and (value < 1 or value > 100):
            await message.answer(texts.t('ADMIN_PROMOCODE_DISCOUNT_RANGE_ERROR', '❌ Процент скидки должен быть от 1 до 100'))
            return

        await state.update_data(promocode_value=value)

        await message.answer(
            texts.t(
                'ADMIN_PROMOCODE_INPUT_USES',
                '📊 Введите количество использований промокода (или 0 для безлимита):',
            )
        )
        await state.set_state(AdminStates.setting_promocode_uses)

    except ValueError:
        await message.answer(texts.t('ADMIN_PROMOCODE_INVALID_NUMBER', '❌ Введите корректное число'))


async def handle_edit_value(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    promo_id = data.get('editing_promo_id')
    edit_action = data.get('edit_action')

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await message.answer(texts.t('ADMIN_PROMOCODE_NOT_FOUND', '❌ Промокод не найден'))
        await state.clear()
        return

    try:
        value = int(message.text.strip())

        if edit_action == 'amount':
            if value < 1 or value > 10000:
                await message.answer(
                    texts.t('ADMIN_PROMOCODE_AMOUNT_RANGE_ERROR', '❌ Сумма должна быть от 1 до 10,000 рублей')
                )
                return

            await update_promocode(db, promo, balance_bonus_kopeks=value * 100)
            await message.answer(
                texts.t('ADMIN_PROMOCODE_AMOUNT_UPDATED', '✅ Сумма бонуса изменена на {value}₽').format(value=value),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_PROMOCODE_BACK_TO_PROMOCODE', '🎫 К промокоду'),
                                callback_data=f'promo_manage_{promo_id}',
                            )
                        ]
                    ]
                ),
            )

        elif edit_action == 'days':
            if value < 1 or value > 3650:
                await message.answer(texts.t('ADMIN_PROMOCODE_DAYS_RANGE_ERROR', '❌ Количество дней должно быть от 1 до 3650'))
                return

            await update_promocode(db, promo, subscription_days=value)
            await message.answer(
                texts.t('ADMIN_PROMOCODE_DAYS_UPDATED', '✅ Количество дней изменено на {value}').format(value=value),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_PROMOCODE_BACK_TO_PROMOCODE', '🎫 К промокоду'),
                                callback_data=f'promo_manage_{promo_id}',
                            )
                        ]
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
        await message.answer(texts.t('ADMIN_PROMOCODE_INVALID_NUMBER', '❌ Введите корректное число'))


@admin_required
@error_handler
async def process_promocode_uses(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()

    if data.get('editing_promo_id'):
        await handle_edit_uses(message, db_user, state, db)
        return

    try:
        max_uses = int(message.text.strip())

        if max_uses < 0 or max_uses > 100000:
            await message.answer(
                texts.t('ADMIN_PROMOCODE_USES_RANGE_ERROR', '❌ Количество использований должно быть от 0 до 100,000')
            )
            return

        if max_uses == 0:
            max_uses = 999999

        await state.update_data(promocode_max_uses=max_uses)

        await message.answer(
            texts.t('ADMIN_PROMOCODE_INPUT_EXPIRY_DAYS', '⏰ Введите срок действия промокода в днях (или 0 для бессрочного):')
        )
        await state.set_state(AdminStates.setting_promocode_expiry)

    except ValueError:
        await message.answer(texts.t('ADMIN_PROMOCODE_INVALID_NUMBER', '❌ Введите корректное число'))


async def handle_edit_uses(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    promo_id = data.get('editing_promo_id')

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await message.answer(texts.t('ADMIN_PROMOCODE_NOT_FOUND', '❌ Промокод не найден'))
        await state.clear()
        return

    try:
        max_uses = int(message.text.strip())

        if max_uses < 0 or max_uses > 100000:
            await message.answer(
                texts.t('ADMIN_PROMOCODE_USES_RANGE_ERROR', '❌ Количество использований должно быть от 0 до 100,000')
            )
            return

        if max_uses == 0:
            max_uses = 999999

        if max_uses < promo.current_uses:
            await message.answer(
                texts.t(
                    'ADMIN_PROMOCODE_USES_LESS_THAN_CURRENT',
                    '❌ Новый лимит ({max_uses}) не может быть меньше текущих использований ({current_uses})',
                ).format(max_uses=max_uses, current_uses=promo.current_uses)
            )
            return

        await update_promocode(db, promo, max_uses=max_uses)

        uses_text = texts.t('ADMIN_PROMOCODE_USES_UNLIMITED', 'безлимитное') if max_uses == 999999 else str(max_uses)
        await message.answer(
            texts.t('ADMIN_PROMOCODE_USES_UPDATED', '✅ Максимальное количество использований изменено на {uses}').format(
                uses=uses_text
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_PROMOCODE_BACK_TO_PROMOCODE', '🎫 К промокоду'),
                            callback_data=f'promo_manage_{promo_id}',
                        )
                    ]
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
        await message.answer(texts.t('ADMIN_PROMOCODE_INVALID_NUMBER', '❌ Введите корректное число'))


@admin_required
@error_handler
async def process_promocode_expiry(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()

    if data.get('editing_promo_id'):
        await handle_edit_expiry(message, db_user, state, db)
        return

    try:
        expiry_days = int(message.text.strip())

        if expiry_days < 0 or expiry_days > 3650:
            await message.answer(
                texts.t('ADMIN_PROMOCODE_EXPIRY_RANGE_ERROR', '❌ Срок действия должен быть от 0 до 3650 дней')
            )
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
                texts.t(
                    'ADMIN_PROMOCODE_INPUT_DISCOUNT_HOURS',
                    '⏰ <b>Промокод:</b> <code>{code}</code>\n\n'
                    'Введите срок действия скидки в часах (0-8760):\n'
                    '0 = бессрочно до первой покупки',
                ).format(code=code)
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

        lines = [
            texts.t('ADMIN_PROMOCODE_CREATED_TITLE', '✅ <b>Промокод создан!</b>'),
            '',
            texts.t('ADMIN_PROMOCODE_CREATED_CODE', '🎫 <b>Код:</b> <code>{code}</code>').format(code=promocode.code),
            texts.t('ADMIN_PROMOCODE_CREATED_TYPE', '📝 <b>Тип:</b> {type_label}').format(
                type_label=_get_promocode_type_label(texts, promo_type)
            ),
        ]

        if promo_type == 'balance':
            lines.append(
                texts.t('ADMIN_PROMOCODE_CREATED_AMOUNT', '💰 <b>Сумма:</b> {amount}').format(
                    amount=settings.format_price(promocode.balance_bonus_kopeks)
                )
            )
        elif promo_type in ['days', 'trial']:
            lines.append(
                texts.t('ADMIN_PROMOCODE_MANAGEMENT_DAYS', '📅 <b>Дней:</b> {days}').format(days=promocode.subscription_days)
            )
        elif promo_type == 'group' and promo_group_name:
            lines.append(
                texts.t('ADMIN_PROMOCODE_CREATED_GROUP', '🏷️ <b>Промогруппа:</b> {name}').format(name=promo_group_name)
            )

        lines.append(
            texts.t('ADMIN_PROMOCODE_CREATED_USES', '📊 <b>Использований:</b> {uses}').format(uses=promocode.max_uses)
        )

        if promocode.valid_until:
            lines.append(
                texts.t('ADMIN_PROMOCODE_MANAGEMENT_VALID_UNTIL', '⏰ <b>Действует до:</b> {date}').format(
                    date=format_datetime(promocode.valid_until)
                )
            )

        summary_text = '\n'.join(lines)

        await message.answer(
            summary_text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_PROMOCODE_BACK_TO_PROMOCODES', '🎫 К промокодам'),
                            callback_data='admin_promocodes',
                        )
                    ]
                ]
            ),
        )

        await state.clear()
        logger.info('Создан промокод администратором', code=code, telegram_id=db_user.telegram_id)

    except ValueError:
        await message.answer(texts.t('ADMIN_PROMOCODE_INVALID_DAYS_NUMBER', '❌ Введите корректное число дней'))


@admin_required
@error_handler
async def process_discount_hours(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    """Обработчик ввода срока действия скидки в часах для DISCOUNT промокода."""
    texts = get_texts(db_user.language)
    data = await state.get_data()

    try:
        discount_hours = int(message.text.strip())

        if discount_hours < 0 or discount_hours > 8760:
            await message.answer(
                texts.t('ADMIN_PROMOCODE_DISCOUNT_HOURS_RANGE_ERROR', '❌ Срок действия скидки должен быть от 0 до 8760 часов')
            )
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

        lines = [
            texts.t('ADMIN_PROMOCODE_CREATED_TITLE', '✅ <b>Промокод создан!</b>'),
            '',
            texts.t('ADMIN_PROMOCODE_CREATED_CODE', '🎫 <b>Код:</b> <code>{code}</code>').format(code=promocode.code),
            texts.t('ADMIN_PROMOCODE_CREATED_TYPE', '📝 <b>Тип:</b> {type_label}').format(
                type_label=_get_promocode_type_label(texts, 'discount')
            ),
            texts.t('ADMIN_PROMOCODE_CREATED_DISCOUNT', '💸 <b>Скидка:</b> {percent}%').format(
                percent=promocode.balance_bonus_kopeks
            ),
        ]

        if discount_hours > 0:
            lines.append(
                texts.t('ADMIN_PROMOCODE_CREATED_DISCOUNT_HOURS', '⏰ <b>Срок скидки:</b> {hours} ч.').format(
                    hours=discount_hours
                )
            )
        else:
            lines.append(
                texts.t(
                    'ADMIN_PROMOCODE_CREATED_DISCOUNT_BEFORE_FIRST_PURCHASE',
                    '⏰ <b>Срок скидки:</b> до первой покупки',
                )
            )

        lines.append(
            texts.t('ADMIN_PROMOCODE_CREATED_USES', '📊 <b>Использований:</b> {uses}').format(uses=promocode.max_uses)
        )

        if promocode.valid_until:
            lines.append(
                texts.t('ADMIN_PROMOCODE_CREATED_DISCOUNT_VALID_UNTIL', '⏳ <b>Промокод действует до:</b> {date}').format(
                    date=format_datetime(promocode.valid_until)
                )
            )

        summary_text = '\n'.join(lines)

        await message.answer(
            summary_text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_PROMOCODE_BACK_TO_PROMOCODES', '🎫 К промокодам'),
                            callback_data='admin_promocodes',
                        )
                    ]
                ]
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
        await message.answer(texts.t('ADMIN_PROMOCODE_INVALID_HOURS_NUMBER', '❌ Введите корректное число часов'))


async def handle_edit_expiry(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    promo_id = data.get('editing_promo_id')

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await message.answer(texts.t('ADMIN_PROMOCODE_NOT_FOUND', '❌ Промокод не найден'))
        await state.clear()
        return

    try:
        expiry_days = int(message.text.strip())

        if expiry_days < 0 or expiry_days > 3650:
            await message.answer(
                texts.t('ADMIN_PROMOCODE_EXPIRY_RANGE_ERROR', '❌ Срок действия должен быть от 0 до 3650 дней')
            )
            return

        valid_until = None
        if expiry_days > 0:
            valid_until = datetime.now(UTC) + timedelta(days=expiry_days)

        await update_promocode(db, promo, valid_until=valid_until)

        if valid_until:
            expiry_text = texts.t('ADMIN_PROMOCODE_EXPIRY_UNTIL', 'до {date}').format(date=format_datetime(valid_until))
        else:
            expiry_text = texts.t('ADMIN_PROMOCODE_EXPIRY_UNLIMITED', 'бессрочно')

        await message.answer(
            texts.t('ADMIN_PROMOCODE_EXPIRY_UPDATED', '✅ Срок действия промокода изменен: {expiry}').format(
                expiry=expiry_text
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_PROMOCODE_BACK_TO_PROMOCODE', '🎫 К промокоду'),
                            callback_data=f'promo_manage_{promo_id}',
                        )
                    ]
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
        await message.answer(texts.t('ADMIN_PROMOCODE_INVALID_DAYS_NUMBER', '❌ Введите корректное число дней'))


@admin_required
@error_handler
async def toggle_promocode_status(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    promo_id = int(callback.data.split('_')[-1])

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer(texts.t('ADMIN_PROMOCODE_NOT_FOUND', '❌ Промокод не найден'), show_alert=True)
        return

    new_status = not promo.is_active
    await update_promocode(db, promo, is_active=new_status)

    status_text = (
        texts.t('ADMIN_PROMOCODE_STATUS_ACTIVATED', 'активирован')
        if new_status
        else texts.t('ADMIN_PROMOCODE_STATUS_DEACTIVATED', 'деактивирован')
    )
    await callback.answer(
        texts.t('ADMIN_PROMOCODE_STATUS_TOGGLED', '✅ Промокод {status}').format(status=status_text),
        show_alert=True,
    )

    await show_promocode_management(callback, db_user, db)


@admin_required
@error_handler
async def toggle_promocode_first_purchase(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Переключает режим 'только для первой покупки'."""
    texts = get_texts(db_user.language)
    promo_id = int(callback.data.split('_')[-1])

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer(texts.t('ADMIN_PROMOCODE_NOT_FOUND', '❌ Промокод не найден'), show_alert=True)
        return

    new_status = not getattr(promo, 'first_purchase_only', False)
    await update_promocode(db, promo, first_purchase_only=new_status)

    status_text = (
        texts.t('ADMIN_PROMOCODE_FIRST_PURCHASE_MODE_ENABLED', 'включён')
        if new_status
        else texts.t('AUTOPAY_STATUS_DISABLED', 'выключен')
    )
    await callback.answer(
        texts.t("ADMIN_PROMOCODE_FIRST_PURCHASE_MODE_TOGGLED", "✅ Режим 'первая покупка' {status}").format(
            status=status_text
        ),
        show_alert=True,
    )

    await show_promocode_management(callback, db_user, db)


@admin_required
@error_handler
async def confirm_delete_promocode(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_PROMOCODE_ID_PARSE_ERROR', '❌ Ошибка получения ID промокода'), show_alert=True)
        return

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer(texts.t('ADMIN_PROMOCODE_NOT_FOUND', '❌ Промокод не найден'), show_alert=True)
        return

    status_text = (
        texts.t('ADMIN_PROMOCODE_STATUS_ACTIVE', 'Активен')
        if promo.is_active
        else texts.t('ADMIN_PROMOCODE_STATUS_INACTIVE', 'Неактивен')
    )
    text = texts.t(
        'ADMIN_PROMOCODE_DELETE_CONFIRM_TEXT',
        '⚠️ <b>Подтверждение удаления</b>\n\n'
        'Вы действительно хотите удалить промокод <code>{code}</code>?\n\n'
        '📊 <b>Информация о промокоде:</b>\n'
        '• Использований: {current_uses}/{max_uses}\n'
        '• Статус: {status}\n\n'
        '<b>⚠️ Внимание:</b> Это действие нельзя отменить!\n\n'
        'ID: {promo_id}',
    ).format(code=promo.code, current_uses=promo.current_uses, max_uses=promo.max_uses, status=status_text, promo_id=promo_id)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_BACKUP_DELETE_CONFIRM_BUTTON', '✅ Да, удалить'),
                    callback_data=f'promo_delete_confirm_{promo.id}',
                ),
                types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'promo_manage_{promo.id}'),
            ]
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def delete_promocode_confirmed(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_PROMOCODE_ID_PARSE_ERROR', '❌ Ошибка получения ID промокода'), show_alert=True)
        return

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer(texts.t('ADMIN_PROMOCODE_NOT_FOUND', '❌ Промокод не найден'), show_alert=True)
        return

    code = promo.code
    success = await delete_promocode(db, promo)

    if success:
        await callback.answer(
            texts.t('ADMIN_PROMOCODE_DELETED', '✅ Промокод {code} удален').format(code=code),
            show_alert=True,
        )
        await show_promocodes_list(callback, db_user, db)
    else:
        await callback.answer(texts.t('ADMIN_PROMOCODE_DELETE_ERROR', '❌ Ошибка удаления промокода'), show_alert=True)


@admin_required
@error_handler
async def show_promocode_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    promo_id = int(callback.data.split('_')[-1])

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer(texts.t('ADMIN_PROMOCODE_NOT_FOUND', '❌ Промокод не найден'), show_alert=True)
        return

    stats = await get_promocode_statistics(db, promo_id)

    text = (
        texts.t('ADMIN_PROMOCODE_STATS_TEXT', '📊 <b>Статистика промокода</b> <code>{code}</code>').format(code=promo.code)
        + '\n\n'
        + texts.t('ADMIN_PROMOCODE_STATS_OVERVIEW', '📈 <b>Общая статистика:</b>')
        + '\n'
        + texts.t('ADMIN_PROMOCODE_STATS_TOTAL_USES', '- Всего использований: {count}').format(count=stats['total_uses'])
        + '\n'
        + texts.t('ADMIN_PROMOCODE_STATS_TODAY_USES', '- Использований сегодня: {count}').format(
            count=stats['today_uses']
        )
        + '\n'
        + texts.t('ADMIN_PROMOCODE_STATS_REMAINING_USES', '- Осталось использований: {count}').format(
            count=promo.max_uses - promo.current_uses
        )
        + '\n\n'
        + texts.t('ADMIN_PROMOCODE_STATS_RECENT_USES', '📅 <b>Последние использования:</b>')
        + '\n'
    )

    if stats['recent_uses']:
        for use in stats['recent_uses'][:5]:
            use_date = format_datetime(use.used_at)

            if hasattr(use, 'user_username') and use.user_username:
                user_display = f'@{use.user_username}'
            elif hasattr(use, 'user_full_name') and use.user_full_name:
                user_display = use.user_full_name
            elif hasattr(use, 'user_telegram_id'):
                user_display = f'ID{use.user_telegram_id}'
            else:
                user_display = f'ID{use.user_id}'

            text += f'- {use_date} | {user_display}\n'
    else:
        text += texts.t('ADMIN_PROMOCODE_STATS_NO_USES', '- Пока не было использований') + '\n'

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data=f'promo_manage_{promo.id}')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def show_general_promocode_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    total_codes = await get_promocodes_count(db)
    active_codes = await get_promocodes_count(db, is_active=True)

    text = (
        texts.t('ADMIN_PROMOCODES_GENERAL_STATS_TITLE_TEXT', '📊 <b>Общая статистика промокодов</b>')
        + '\n\n'
        + texts.t('ADMIN_PROMOCODES_GENERAL_STATS_MAIN_METRICS', '📈 <b>Основные показатели:</b>')
        + '\n'
        + texts.t('ADMIN_PROMOCODES_MENU_TOTAL_LINE', '- Всего промокодов: {count}').format(count=total_codes)
        + '\n'
        + texts.t('ADMIN_PROMOCODES_MENU_ACTIVE_LINE', '- Активных: {count}').format(count=active_codes)
        + '\n'
        + texts.t('ADMIN_PROMOCODES_MENU_INACTIVE_LINE', '- Неактивных: {count}').format(count=total_codes - active_codes)
        + '\n\n'
        + texts.t(
            'ADMIN_PROMOCODES_GENERAL_STATS_HINT',
            'Для детальной статистики выберите конкретный промокод из списка.',
        )
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMOCODE_BACK_TO_PROMOCODES', '🎫 К промокодам'),
                    callback_data='admin_promo_list',
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_promocodes')],
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
