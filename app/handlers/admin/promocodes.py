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
from app.localization.texts import get_texts
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler
from app.utils.formatters import format_datetime


logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_promocodes_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    total_codes = await get_promocodes_count(db)
    active_codes = await get_promocodes_count(db, is_active=True)

    text = (
        get_texts(db_user.language)
        .t(
            'ADMIN_PROMOCODE_UI_000',
            '\n🎫 <b>Управление промокодами</b>\n\n📊 <b>Статистика:</b>\n- Всего промокодов: {v0}\n- Активных: {v1}\n- Неактивных: {v2}\n\nВыберите действие:\n',
        )
        .format(v0=total_codes, v1=active_codes, v2=total_codes - active_codes)
    )

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
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_060', '🎫 Промокоды не найдены'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_102', '⬅️ Назад'),
                            callback_data='admin_promocodes',
                        )
                    ]
                ]
            ),
        )
        await callback.answer()
        return

    text = (
        get_texts(db_user.language)
        .t('ADMIN_PROMOCODE_UI_001', '🎫 <b>Список промокодов</b> (стр. {v0}/{v1})\n\n')
        .format(v0=page, v1=total_pages)
    )
    keyboard = []

    for promo in promocodes:
        status_emoji = '✅' if promo.is_active else '❌'
        type_emoji = {
            'balance': '💰',
            'subscription_days': '📅',
            'trial_subscription': '🎁',
            'promo_group': '🏷️',
            'discount': '💸',
            'balance_and_days': '💰📅',
        }.get(promo.type, '🎫')

        text += f'{status_emoji} {type_emoji} <code>{promo.code}</code>\n'
        text += (
            get_texts(db_user.language)
            .t('ADMIN_PROMOCODE_UI_015', '📊 Использований: {v0}/{v1}\n')
            .format(v0=promo.current_uses, v1=promo.max_uses)
        )

        if promo.type == PromoCodeType.BALANCE.value:
            text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_038', '💰 Бонус: {v0}\n')
                .format(v0=settings.format_price(promo.balance_bonus_kopeks))
            )
        elif promo.type == PromoCodeType.SUBSCRIPTION_DAYS.value:
            text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_061', '📅 Дней: {v0}\n')
                .format(v0=promo.subscription_days)
            )
        elif promo.type == PromoCodeType.BALANCE_AND_DAYS.value:
            if promo.balance_bonus_kopeks:
                text += (
                    get_texts(db_user.language)
                    .t('ADMIN_PROMOCODE_UI_038', '💰 Бонус: {v0}\n')
                    .format(v0=settings.format_price(promo.balance_bonus_kopeks))
                )
            if promo.subscription_days:
                text += (
                    get_texts(db_user.language)
                    .t('ADMIN_PROMOCODE_UI_061', '📅 Дней: {v0}\n')
                    .format(v0=promo.subscription_days)
                )
            if getattr(promo, 'traffic_gb', 0):
                text += (
                    get_texts(db_user.language)
                    .t('ADMIN_PROMOCODE_UI_100', '📦 Трафик: {v0} ГБ\n')
                    .format(v0=promo.traffic_gb)
                )
        elif promo.type == PromoCodeType.PROMO_GROUP.value:
            if promo.promo_group:
                text += (
                    get_texts(db_user.language)
                    .t('ADMIN_PROMOCODE_UI_039', '🏷️ Промогруппа: {v0}\n')
                    .format(v0=html.escape(promo.promo_group.name))
                )
        elif promo.type == PromoCodeType.DISCOUNT.value:
            discount_hours = promo.subscription_days
            if discount_hours > 0:
                text += (
                    get_texts(db_user.language)
                    .t('ADMIN_PROMOCODE_UI_116', '💸 Скидка: {v0}% ({v1} ч.)\n')
                    .format(v0=promo.balance_bonus_kopeks, v1=discount_hours)
                )
            else:
                text += (
                    get_texts(db_user.language)
                    .t('ADMIN_PROMOCODE_UI_117', '💸 Скидка: {v0}% (до покупки)\n')
                    .format(v0=promo.balance_bonus_kopeks)
                )

        # Промогруппа комбинируется с любым типом (назначается при активации
        # независимо от type) — показываем прикреплённую группу и у составных
        if promo.type != PromoCodeType.PROMO_GROUP.value and promo.promo_group:
            text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_039', '🏷️ Промогруппа: {v0}\n')
                .format(v0=html.escape(promo.promo_group.name))
            )

        if promo.valid_until:
            text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_040', '⏰ До: {v0}\n')
                .format(v0=format_datetime(promo.valid_until))
            )

        keyboard.append([types.InlineKeyboardButton(text=f'🎫 {promo.code}', callback_data=f'promo_manage_{promo.id}')])

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
                    text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_101', '➕ Создать'),
                    callback_data='admin_promo_create',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_102', '⬅️ Назад'),
                    callback_data='admin_promocodes',
                )
            ],
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
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_062', '❌ Промокод не найден'), show_alert=True
        )
        return

    status_emoji = '✅' if promo.is_active else '❌'
    type_emoji = {
        'balance': '💰',
        'subscription_days': '📅',
        'trial_subscription': '🎁',
        'promo_group': '🏷️',
        'discount': '💸',
        'balance_and_days': '💰📅',
    }.get(promo.type, '🎫')

    text = (
        get_texts(db_user.language)
        .t(
            'ADMIN_PROMOCODE_UI_002',
            '\n🎫 <b>Управление промокодом</b>\n\n{v0} <b>Код:</b> <code>{v1}</code>\n{v2} <b>Статус:</b> {v3}\n📊 <b>Использований:</b> {v4}/{v5}\n',
        )
        .format(
            v0=type_emoji,
            v1=promo.code,
            v2=status_emoji,
            v3=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_063', 'Активен')
            if promo.is_active
            else get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_064', 'Неактивен'),
            v4=promo.current_uses,
            v5=promo.max_uses,
        )
    )

    if promo.type == PromoCodeType.BALANCE.value:
        text += (
            get_texts(db_user.language)
            .t('ADMIN_PROMOCODE_UI_016', '💰 <b>Бонус:</b> {v0}\n')
            .format(v0=settings.format_price(promo.balance_bonus_kopeks))
        )
    elif promo.type == PromoCodeType.SUBSCRIPTION_DAYS.value:
        text += (
            get_texts(db_user.language)
            .t('ADMIN_PROMOCODE_UI_041', '📅 <b>Дней:</b> {v0}\n')
            .format(v0=promo.subscription_days)
        )
    elif promo.type == PromoCodeType.BALANCE_AND_DAYS.value:
        if promo.balance_bonus_kopeks:
            text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_016', '💰 <b>Бонус:</b> {v0}\n')
                .format(v0=settings.format_price(promo.balance_bonus_kopeks))
            )
        if promo.subscription_days:
            text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_041', '📅 <b>Дней:</b> {v0}\n')
                .format(v0=promo.subscription_days)
            )
        if getattr(promo, 'traffic_gb', 0):
            text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_075', '📦 <b>Трафик:</b> {v0} ГБ\n')
                .format(v0=promo.traffic_gb)
            )
    elif promo.type == PromoCodeType.PROMO_GROUP.value:
        if promo.promo_group:
            text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_103', '🏷️ <b>Промогруппа:</b> {v0} (приоритет: {v1})\n')
                .format(v0=html.escape(promo.promo_group.name), v1=promo.promo_group.priority)
            )
        elif promo.promo_group_id:
            text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_107', '🏷️ <b>Промогруппа ID:</b> {v0} (не найдена)\n')
                .format(v0=promo.promo_group_id)
            )
    elif promo.type == PromoCodeType.DISCOUNT.value:
        discount_hours = promo.subscription_days
        if discount_hours > 0:
            text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_108', '💸 <b>Скидка:</b> {v0}% (срок: {v1} ч.)\n')
                .format(v0=promo.balance_bonus_kopeks, v1=discount_hours)
            )
        else:
            text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_109', '💸 <b>Скидка:</b> {v0}% (до первой покупки)\n')
                .format(v0=promo.balance_bonus_kopeks)
            )

    # Промогруппа комбинируется с любым типом (назначается при активации
    # независимо от type) — показываем прикреплённую группу и у составных
    if promo.type != PromoCodeType.PROMO_GROUP.value and promo.promo_group:
        text += (
            get_texts(db_user.language)
            .t('ADMIN_PROMOCODE_UI_017', '🏷️ <b>Промогруппа:</b> {v0}\n')
            .format(v0=html.escape(promo.promo_group.name))
        )

    if promo.valid_until:
        text += (
            get_texts(db_user.language)
            .t('ADMIN_PROMOCODE_UI_018', '⏰ <b>Действует до:</b> {v0}\n')
            .format(v0=format_datetime(promo.valid_until))
        )

    first_purchase_only = getattr(promo, 'first_purchase_only', False)
    first_purchase_emoji = '✅' if first_purchase_only else '❌'
    text += (
        get_texts(db_user.language)
        .t('ADMIN_PROMOCODE_UI_003', '🆕 <b>Только первая покупка:</b> {v0}\n')
        .format(v0=first_purchase_emoji)
    )

    text += (
        get_texts(db_user.language)
        .t('ADMIN_PROMOCODE_UI_004', '📅 <b>Создан:</b> {v0}\n')
        .format(v0=format_datetime(promo.created_at))
    )

    first_purchase_btn_text = (
        get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_019', '🆕 Первая покупка: ✅')
        if first_purchase_only
        else get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_020', '🆕 Первая покупка: ❌')
    )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_076', '✏️ Редактировать'),
                callback_data=f'promo_edit_{promo.id}',
            ),
            types.InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_077', '🔄 Переключить статус'),
                callback_data=f'promo_toggle_{promo.id}',
            ),
        ],
        [types.InlineKeyboardButton(text=first_purchase_btn_text, callback_data=f'promo_toggle_first_{promo.id}')],
        [
            types.InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_078', '📊 Статистика'),
                callback_data=f'promo_stats_{promo.id}',
            ),
            types.InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_079', '🗑️ Удалить'),
                callback_data=f'promo_delete_{promo.id}',
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_080', '⬅️ К списку'),
                callback_data='admin_promo_list',
            )
        ],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_promocode_edit_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_081', '❌ Ошибка получения ID промокода'), show_alert=True
        )
        return

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_062', '❌ Промокод не найден'), show_alert=True
        )
        return

    text = (
        get_texts(db_user.language)
        .t(
            'ADMIN_PROMOCODE_UI_005',
            '\n✏️ <b>Редактирование промокода</b> <code>{v0}</code>\n\n💰 <b>Текущие параметры:</b>\n',
        )
        .format(v0=promo.code)
    )

    if promo.type == PromoCodeType.BALANCE.value:
        text += (
            get_texts(db_user.language)
            .t('ADMIN_PROMOCODE_UI_021', '• Бонус: {v0}\n')
            .format(v0=settings.format_price(promo.balance_bonus_kopeks))
        )
    elif promo.type in [PromoCodeType.SUBSCRIPTION_DAYS.value, PromoCodeType.TRIAL_SUBSCRIPTION.value]:
        text += (
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_042', '• Дней: {v0}\n').format(v0=promo.subscription_days)
        )
    elif promo.type == PromoCodeType.BALANCE_AND_DAYS.value:
        if promo.balance_bonus_kopeks:
            text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_021', '• Бонус: {v0}\n')
                .format(v0=settings.format_price(promo.balance_bonus_kopeks))
            )
        if promo.subscription_days:
            text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_042', '• Дней: {v0}\n')
                .format(v0=promo.subscription_days)
            )
        if getattr(promo, 'traffic_gb', 0):
            text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_082', '• Трафик: {v0} ГБ\n')
                .format(v0=promo.traffic_gb)
            )

    text += (
        get_texts(db_user.language)
        .t('ADMIN_PROMOCODE_UI_006', '• Использований: {v0}/{v1}\n')
        .format(v0=promo.current_uses, v1=promo.max_uses)
    )

    if promo.valid_until:
        text += (
            get_texts(db_user.language)
            .t('ADMIN_PROMOCODE_UI_022', '• До: {v0}\n')
            .format(v0=format_datetime(promo.valid_until))
        )
    else:
        text += get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_023', '• Срок: бессрочно\n')

    text += get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_007', '\nВыберите параметр для изменения:')

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_083', '📅 Дата окончания'),
                callback_data=f'promo_edit_date_{promo.id}',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_084', '📊 Количество использований'),
                callback_data=f'promo_edit_uses_{promo.id}',
            )
        ],
    ]

    if promo.type == PromoCodeType.BALANCE.value:
        keyboard.insert(
            1,
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_104', '💰 Сумма бонуса'),
                    callback_data=f'promo_edit_amount_{promo.id}',
                )
            ],
        )
    elif promo.type in [PromoCodeType.SUBSCRIPTION_DAYS.value, PromoCodeType.TRIAL_SUBSCRIPTION.value]:
        keyboard.insert(
            1,
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_110', '📅 Количество дней'),
                    callback_data=f'promo_edit_days_{promo.id}',
                )
            ],
        )
    elif promo.type == PromoCodeType.BALANCE_AND_DAYS.value:
        keyboard.insert(
            1,
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_104', '💰 Сумма бонуса'),
                    callback_data=f'promo_edit_amount_{promo.id}',
                )
            ],
        )
        keyboard.insert(
            2,
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_110', '📅 Количество дней'),
                    callback_data=f'promo_edit_days_{promo.id}',
                )
            ],
        )

    keyboard.extend(
        [
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_102', '⬅️ Назад'),
                    callback_data=f'promo_manage_{promo.id}',
                )
            ]
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def start_edit_promocode_date(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_081', '❌ Ошибка получения ID промокода'), show_alert=True
        )
        return

    await state.update_data(editing_promo_id=promo_id, edit_action='date')

    text = (
        get_texts(db_user.language)
        .t(
            'ADMIN_PROMOCODE_UI_008',
            '\n📅 <b>Изменение даты окончания промокода</b>\n\nВведите количество дней до окончания (от текущего момента):\n• Введите <b>0</b> для бессрочного промокода\n• Введите положительное число для установки срока\n\n<i>Например: 30 (промокод будет действовать 30 дней)</i>\n\nID промокода: {v0}\n',
        )
        .format(v0=promo_id)
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_111', '❌ Отмена'),
                    callback_data=f'promo_edit_{promo_id}',
                )
            ]
        ]
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
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_081', '❌ Ошибка получения ID промокода'), show_alert=True
        )
        return

    await state.update_data(editing_promo_id=promo_id, edit_action='amount')

    text = (
        get_texts(db_user.language)
        .t(
            'ADMIN_PROMOCODE_UI_009',
            '\n💰 <b>Изменение суммы бонуса промокода</b>\n\nВведите новую сумму в рублях:\n<i>Например: 500</i>\n\nID промокода: {v0}\n',
        )
        .format(v0=promo_id)
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_111', '❌ Отмена'),
                    callback_data=f'promo_edit_{promo_id}',
                )
            ]
        ]
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
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_081', '❌ Ошибка получения ID промокода'), show_alert=True
        )
        return

    await state.update_data(editing_promo_id=promo_id, edit_action='days')

    text = (
        get_texts(db_user.language)
        .t(
            'ADMIN_PROMOCODE_UI_010',
            '\n📅 <b>Изменение количества дней подписки</b>\n\nВведите новое количество дней:\n<i>Например: 30</i>\n\nID промокода: {v0}\n',
        )
        .format(v0=promo_id)
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_111', '❌ Отмена'),
                    callback_data=f'promo_edit_{promo_id}',
                )
            ]
        ]
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
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_081', '❌ Ошибка получения ID промокода'), show_alert=True
        )
        return

    await state.update_data(editing_promo_id=promo_id, edit_action='uses')

    text = (
        get_texts(db_user.language)
        .t(
            'ADMIN_PROMOCODE_UI_011',
            '\n📊 <b>Изменение максимального количества использований</b>\n\nВведите новое количество использований:\n• Введите <b>0</b> для безлимитных использований\n• Введите положительное число для ограничения\n\n<i>Например: 100</i>\n\nID промокода: {v0}\n',
        )
        .format(v0=promo_id)
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_111', '❌ Отмена'),
                    callback_data=f'promo_edit_{promo_id}',
                )
            ]
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await state.set_state(AdminStates.setting_promocode_uses)
    await callback.answer()


@admin_required
@error_handler
async def start_promocode_creation(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    await callback.message.edit_text(
        get_texts(db_user.language).t(
            'ADMIN_PROMOCODE_UI_043', '🎫 <b>Создание промокода</b>\n\nВыберите тип промокода:'
        ),
        reply_markup=get_promocode_type_keyboard(db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def select_promocode_type(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    promo_type = callback.data.split('_')[-1]

    type_names = {
        'balance': get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_024', '💰 Пополнение баланса'),
        'days': get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_025', '📅 Дни подписки'),
        'trial': get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_026', '🎁 Тестовая подписка'),
        'group': get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_027', '🏷️ Промогруппа'),
        'discount': get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_028', '💸 Одноразовая скидка'),
        'combo': get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_029', '💰📅 Баланс + дни подписки'),
    }

    await state.update_data(promocode_type=promo_type)

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_PROMOCODE_UI_044',
            '🎫 <b>Создание промокода</b>\n\nТип: {v0}\n\nВведите код промокода (только латинские буквы и цифры):',
        )
        .format(v0=type_names.get(promo_type, promo_type)),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_111', '❌ Отмена'),
                        callback_data='admin_promocodes',
                    )
                ]
            ]
        ),
    )

    await state.set_state(AdminStates.creating_promocode)
    await callback.answer()


@admin_required
@error_handler
async def process_promocode_code(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    code = message.text.strip().upper()

    if not code.isalnum() or len(code) < 3 or len(code) > 20:
        await message.answer(
            get_texts(db_user.language).t(
                'ADMIN_PROMOCODE_UI_065', '❌ Код должен содержать только латинские буквы и цифры (3-20 символов)'
            )
        )
        return

    existing = await get_promocode_by_code(db, code)
    if existing:
        await message.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_066', '❌ Промокод с таким кодом уже существует')
        )
        return

    await state.update_data(promocode_code=code)

    data = await state.get_data()
    promo_type = data.get('promocode_type')

    if promo_type == 'balance':
        await message.answer(
            get_texts(db_user.language)
            .t(
                'ADMIN_PROMOCODE_UI_067',
                '💰 <b>Промокод:</b> <code>{v0}</code>\n\nВведите сумму пополнения баланса (в рублях):',
            )
            .format(v0=code)
        )
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'combo':
        await message.answer(
            get_texts(db_user.language)
            .t(
                'ADMIN_PROMOCODE_UI_085',
                '💰📅 <b>Промокод:</b> <code>{v0}</code>\n\nШаг 1 из 2: введите сумму пополнения баланса (в рублях), дни подписки спрошу следующим шагом:',
            )
            .format(v0=code)
        )
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'days':
        await message.answer(
            get_texts(db_user.language)
            .t('ADMIN_PROMOCODE_UI_105', '📅 <b>Промокод:</b> <code>{v0}</code>\n\nВведите количество дней подписки:')
            .format(v0=code)
        )
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'trial':
        await message.answer(
            get_texts(db_user.language)
            .t(
                'ADMIN_PROMOCODE_UI_112',
                '🎁 <b>Промокод:</b> <code>{v0}</code>\n\nВведите количество дней тестовой подписки:',
            )
            .format(v0=code)
        )
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'discount':
        await message.answer(
            get_texts(db_user.language)
            .t('ADMIN_PROMOCODE_UI_118', '💸 <b>Промокод:</b> <code>{v0}</code>\n\nВведите процент скидки (1-100):')
            .format(v0=code)
        )
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'group':
        # Show promo group selection
        groups_with_counts = await get_promo_groups_with_counts(db, limit=50)

        if not groups_with_counts:
            await message.answer(
                get_texts(db_user.language).t(
                    'ADMIN_PROMOCODE_UI_120', '❌ Промогруппы не найдены. Создайте хотя бы одну промогруппу.'
                ),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_102', '⬅️ Назад'),
                                callback_data='admin_promocodes',
                            )
                        ]
                    ]
                ),
            )
            await state.clear()
            return

        keyboard = []
        text = (
            get_texts(db_user.language)
            .t(
                'ADMIN_PROMOCODE_UI_113',
                '🏷️ <b>Промокод:</b> <code>{v0}</code>\n\nВыберите промогруппу для назначения:\n\n',
            )
            .format(v0=code)
        )

        for promo_group, user_count in groups_with_counts:
            text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_119', '• {v0} (приоритет: {v1}, пользователей: {v2})\n')
                .format(v0=html.escape(promo_group.name), v1=promo_group.priority, v2=user_count)
            )
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=f'{promo_group.name} (↑{promo_group.priority})',
                        callback_data=f'promo_select_group_{promo_group.id}',
                    )
                ]
            )

        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_111', '❌ Отмена'),
                    callback_data='admin_promocodes',
                )
            ]
        )

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
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_086', '❌ Ошибка получения ID промогруппы'),
            show_alert=True,
        )
        return

    promo_group = await get_promo_group_by_id(db, promo_group_id)
    if not promo_group:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_068', '❌ Промогруппа не найдена'), show_alert=True
        )
        return

    await state.update_data(promo_group_id=promo_group_id, promo_group_name=promo_group.name)

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_PROMOCODE_UI_045',
            '🏷️ <b>Промокод для промогруппы</b>\n\nПромогруппа: {v0}\nПриоритет: {v1}\n\n📊 Введите количество использований промокода (или 0 для безлимита):',
        )
        .format(v0=html.escape(promo_group.name), v1=promo_group.priority)
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

        if promo_type in ['balance', 'combo'] and (value < 1 or value > 10000):
            await message.answer(
                get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_087', '❌ Сумма должна быть от 1 до 10,000 рублей')
            )
            return
        if promo_type in ['days', 'trial'] and (value < 1 or value > 3650):
            await message.answer(
                get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_088', '❌ Количество дней должно быть от 1 до 3650')
            )
            return
        if promo_type == 'discount' and (value < 1 or value > 100):
            await message.answer(
                get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_089', '❌ Процент скидки должен быть от 1 до 100')
            )
            return

        await state.update_data(promocode_value=value)

        # Для комбинированного типа сумма — только первый шаг, дальше дни
        if promo_type == 'combo':
            await message.answer(
                get_texts(db_user.language).t(
                    'ADMIN_PROMOCODE_UI_090', '📅 Шаг 2 из 2: введите количество дней подписки:'
                )
            )
            await state.set_state(AdminStates.setting_promocode_combo_days)
            return

        await message.answer(
            get_texts(db_user.language).t(
                'ADMIN_PROMOCODE_UI_069', '📊 Введите количество использований промокода (или 0 для безлимита):'
            )
        )
        await state.set_state(AdminStates.setting_promocode_uses)

    except ValueError:
        await message.answer(get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_091', '❌ Введите корректное число'))


@admin_required
@error_handler
async def process_promocode_combo_days(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    """Шаг 2 комбинированного промокода (BALANCE_AND_DAYS): ввод дней подписки."""
    try:
        days = int(message.text.strip())

        if days < 1 or days > 3650:
            await message.answer(
                get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_088', '❌ Количество дней должно быть от 1 до 3650')
            )
            return

        await state.update_data(promocode_combo_days=days)

        await message.answer(
            get_texts(db_user.language).t(
                'ADMIN_PROMOCODE_UI_069', '📊 Введите количество использований промокода (или 0 для безлимита):'
            )
        )
        await state.set_state(AdminStates.setting_promocode_uses)

    except ValueError:
        await message.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_092', '❌ Введите корректное число дней')
        )


async def handle_edit_value(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()
    promo_id = data.get('editing_promo_id')
    edit_action = data.get('edit_action')

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await message.answer(get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_062', '❌ Промокод не найден'))
        await state.clear()
        return

    try:
        value = int(message.text.strip())

        if edit_action == 'amount':
            if value < 1 or value > 10000:
                await message.answer(
                    get_texts(db_user.language).t(
                        'ADMIN_PROMOCODE_UI_087', '❌ Сумма должна быть от 1 до 10,000 рублей'
                    )
                )
                return

            await update_promocode(db, promo, balance_bonus_kopeks=value * 100)
            await message.answer(
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_093', '✅ Сумма бонуса изменена на {v0}₽')
                .format(v0=value),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_121', '🎫 К промокоду'),
                                callback_data=f'promo_manage_{promo_id}',
                            )
                        ]
                    ]
                ),
            )

        elif edit_action == 'days':
            if value < 1 or value > 3650:
                await message.answer(
                    get_texts(db_user.language).t(
                        'ADMIN_PROMOCODE_UI_088', '❌ Количество дней должно быть от 1 до 3650'
                    )
                )
                return

            await update_promocode(db, promo, subscription_days=value)
            await message.answer(
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_106', '✅ Количество дней изменено на {v0}')
                .format(v0=value),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_121', '🎫 К промокоду'),
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
        await message.answer(get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_091', '❌ Введите корректное число'))


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
            await message.answer(
                get_texts(db_user.language).t(
                    'ADMIN_PROMOCODE_UI_094', '❌ Количество использований должно быть от 0 до 100,000'
                )
            )
            return

        if max_uses == 0:
            max_uses = 999999

        await state.update_data(promocode_max_uses=max_uses)

        await message.answer(
            get_texts(db_user.language).t(
                'ADMIN_PROMOCODE_UI_070', '⏰ Введите срок действия промокода в днях (или 0 для бессрочного):'
            )
        )
        await state.set_state(AdminStates.setting_promocode_expiry)

    except ValueError:
        await message.answer(get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_091', '❌ Введите корректное число'))


async def handle_edit_uses(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()
    promo_id = data.get('editing_promo_id')

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await message.answer(get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_062', '❌ Промокод не найден'))
        await state.clear()
        return

    try:
        max_uses = int(message.text.strip())

        if max_uses < 0 or max_uses > 100000:
            await message.answer(
                get_texts(db_user.language).t(
                    'ADMIN_PROMOCODE_UI_094', '❌ Количество использований должно быть от 0 до 100,000'
                )
            )
            return

        if max_uses == 0:
            max_uses = 999999

        if max_uses < promo.current_uses:
            await message.answer(
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_095', '❌ Новый лимит ({v0}) не может быть меньше текущих использований ({v1})')
                .format(v0=max_uses, v1=promo.current_uses)
            )
            return

        await update_promocode(db, promo, max_uses=max_uses)

        uses_text = (
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_046', 'безлимитное')
            if max_uses == 999999
            else str(max_uses)
        )
        await message.answer(
            get_texts(db_user.language)
            .t('ADMIN_PROMOCODE_UI_071', '✅ Максимальное количество использований изменено на {v0}')
            .format(v0=uses_text),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_121', '🎫 К промокоду'),
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
        await message.answer(get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_091', '❌ Введите корректное число'))


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
            await message.answer(
                get_texts(db_user.language).t(
                    'ADMIN_PROMOCODE_UI_096', '❌ Срок действия должен быть от 0 до 3650 дней'
                )
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
                get_texts(db_user.language)
                .t(
                    'ADMIN_PROMOCODE_UI_097',
                    '⏰ <b>Промокод:</b> <code>{v0}</code>\n\nВведите срок действия скидки в часах (0-8760):\n0 = бессрочно до первой покупки',
                )
                .format(v0=code)
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
            'combo': PromoCodeType.BALANCE_AND_DAYS,
        }

        if promo_type == 'combo':
            balance_bonus_kopeks = value * 100
            subscription_days = data.get('promocode_combo_days', 0)
        else:
            balance_bonus_kopeks = value * 100 if promo_type == 'balance' else 0
            subscription_days = value if promo_type in ['days', 'trial'] else 0

        promocode = await create_promocode(
            db=db,
            code=code,
            type=type_map[promo_type],
            balance_bonus_kopeks=balance_bonus_kopeks,
            subscription_days=subscription_days,
            max_uses=max_uses,
            valid_until=valid_until,
            created_by=db_user.id,
            promo_group_id=promo_group_id if promo_type == 'group' else None,
        )

        type_names = {
            'balance': get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_047', 'Пополнение баланса'),
            'days': get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_048', 'Дни подписки'),
            'trial': get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_049', 'Тестовая подписка'),
            'group': get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_050', 'Промогруппа'),
            'combo': get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_051', 'Баланс + дни подписки'),
        }

        summary_text = (
            get_texts(db_user.language)
            .t(
                'ADMIN_PROMOCODE_UI_030',
                '\n✅ <b>Промокод создан!</b>\n\n🎫 <b>Код:</b> <code>{v0}</code>\n📝 <b>Тип:</b> {v1}\n',
            )
            .format(v0=promocode.code, v1=type_names.get(promo_type))
        )

        if promo_type == 'balance':
            summary_text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_052', '💰 <b>Сумма:</b> {v0}\n')
                .format(v0=settings.format_price(promocode.balance_bonus_kopeks))
            )
        elif promo_type in ['days', 'trial']:
            summary_text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_041', '📅 <b>Дней:</b> {v0}\n')
                .format(v0=promocode.subscription_days)
            )
        elif promo_type == 'combo':
            summary_text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_052', '💰 <b>Сумма:</b> {v0}\n')
                .format(v0=settings.format_price(promocode.balance_bonus_kopeks))
            )
            summary_text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_041', '📅 <b>Дней:</b> {v0}\n')
                .format(v0=promocode.subscription_days)
            )
        elif promo_type == 'group' and promo_group_name:
            summary_text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_017', '🏷️ <b>Промогруппа:</b> {v0}\n')
                .format(v0=promo_group_name)
            )

        summary_text += (
            get_texts(db_user.language)
            .t('ADMIN_PROMOCODE_UI_031', '📊 <b>Использований:</b> {v0}\n')
            .format(v0=promocode.max_uses)
        )

        if promocode.valid_until:
            summary_text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_018', '⏰ <b>Действует до:</b> {v0}\n')
                .format(v0=format_datetime(promocode.valid_until))
            )

        await message.answer(
            summary_text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_115', '🎫 К промокодам'),
                            callback_data='admin_promocodes',
                        )
                    ]
                ]
            ),
        )

        await state.clear()
        logger.info('Создан промокод администратором', code=code, telegram_id=db_user.telegram_id)

    except ValueError:
        await message.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_092', '❌ Введите корректное число дней')
        )


@admin_required
@error_handler
async def process_discount_hours(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    """Обработчик ввода срока действия скидки в часах для DISCOUNT промокода."""
    data = await state.get_data()

    try:
        discount_hours = int(message.text.strip())

        if discount_hours < 0 or discount_hours > 8760:
            await message.answer(
                get_texts(db_user.language).t(
                    'ADMIN_PROMOCODE_UI_098', '❌ Срок действия скидки должен быть от 0 до 8760 часов'
                )
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

        summary_text = (
            get_texts(db_user.language)
            .t(
                'ADMIN_PROMOCODE_UI_032',
                '\n✅ <b>Промокод создан!</b>\n\n🎫 <b>Код:</b> <code>{v0}</code>\n📝 <b>Тип:</b> Одноразовая скидка\n💸 <b>Скидка:</b> {v1}%\n',
            )
            .format(v0=promocode.code, v1=promocode.balance_bonus_kopeks)
        )

        if discount_hours > 0:
            summary_text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_053', '⏰ <b>Срок скидки:</b> {v0} ч.\n')
                .format(v0=discount_hours)
            )
        else:
            summary_text += get_texts(db_user.language).t(
                'ADMIN_PROMOCODE_UI_054', '⏰ <b>Срок скидки:</b> до первой покупки\n'
            )

        summary_text += (
            get_texts(db_user.language)
            .t('ADMIN_PROMOCODE_UI_031', '📊 <b>Использований:</b> {v0}\n')
            .format(v0=promocode.max_uses)
        )

        if promocode.valid_until:
            summary_text += (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_055', '⏳ <b>Промокод действует до:</b> {v0}\n')
                .format(v0=format_datetime(promocode.valid_until))
            )

        await message.answer(
            summary_text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_115', '🎫 К промокодам'),
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
        await message.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_099', '❌ Введите корректное число часов')
        )


async def handle_edit_expiry(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()
    promo_id = data.get('editing_promo_id')

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await message.answer(get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_062', '❌ Промокод не найден'))
        await state.clear()
        return

    try:
        expiry_days = int(message.text.strip())

        if expiry_days < 0 or expiry_days > 3650:
            await message.answer(
                get_texts(db_user.language).t(
                    'ADMIN_PROMOCODE_UI_096', '❌ Срок действия должен быть от 0 до 3650 дней'
                )
            )
            return

        valid_until = None
        if expiry_days > 0:
            valid_until = datetime.now(UTC) + timedelta(days=expiry_days)

        await update_promocode(db, promo, valid_until=valid_until)

        if valid_until:
            expiry_text = (
                get_texts(db_user.language)
                .t('ADMIN_PROMOCODE_UI_056', 'до {v0}')
                .format(v0=format_datetime(valid_until))
            )
        else:
            expiry_text = get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_057', 'бессрочно')

        await message.answer(
            get_texts(db_user.language)
            .t('ADMIN_PROMOCODE_UI_072', '✅ Срок действия промокода изменен: {v0}')
            .format(v0=expiry_text),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_121', '🎫 К промокоду'),
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
        await message.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_092', '❌ Введите корректное число дней')
        )


@admin_required
@error_handler
async def toggle_promocode_status(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    promo_id = int(callback.data.split('_')[-1])

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_062', '❌ Промокод не найден'), show_alert=True
        )
        return

    new_status = not promo.is_active
    await update_promocode(db, promo, is_active=new_status)

    status_text = (
        get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_033', 'активирован')
        if new_status
        else get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_034', 'деактивирован')
    )
    await callback.answer(
        get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_058', '✅ Промокод {v0}').format(v0=status_text),
        show_alert=True,
    )

    await show_promocode_management(callback, db_user, db)


@admin_required
@error_handler
async def toggle_promocode_first_purchase(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Переключает режим 'только для первой покупки'."""
    promo_id = int(callback.data.split('_')[-1])

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_062', '❌ Промокод не найден'), show_alert=True
        )
        return

    new_status = not getattr(promo, 'first_purchase_only', False)
    await update_promocode(db, promo, first_purchase_only=new_status)

    status_text = (
        get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_035', 'включён')
        if new_status
        else get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_036', 'выключен')
    )
    await callback.answer(
        get_texts(db_user.language)
        .t('ADMIN_PROMOCODE_UI_059', "✅ Режим 'первая покупка' {v0}")
        .format(v0=status_text),
        show_alert=True,
    )

    await show_promocode_management(callback, db_user, db)


@admin_required
@error_handler
async def confirm_delete_promocode(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_081', '❌ Ошибка получения ID промокода'), show_alert=True
        )
        return

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_062', '❌ Промокод не найден'), show_alert=True
        )
        return

    text = (
        get_texts(db_user.language)
        .t(
            'ADMIN_PROMOCODE_UI_012',
            '\n⚠️ <b>Подтверждение удаления</b>\n\nВы действительно хотите удалить промокод <code>{v0}</code>?\n\n📊 <b>Информация о промокоде:</b>\n• Использований: {v1}/{v2}\n• Статус: {v3}\n\n<b>⚠️ Внимание:</b> Это действие нельзя отменить!\n\nID: {v4}\n',
        )
        .format(
            v0=promo.code,
            v1=promo.current_uses,
            v2=promo.max_uses,
            v3=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_063', 'Активен')
            if promo.is_active
            else get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_064', 'Неактивен'),
            v4=promo_id,
        )
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_114', '✅ Да, удалить'),
                    callback_data=f'promo_delete_confirm_{promo.id}',
                ),
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_111', '❌ Отмена'),
                    callback_data=f'promo_manage_{promo.id}',
                ),
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
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_081', '❌ Ошибка получения ID промокода'), show_alert=True
        )
        return

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_062', '❌ Промокод не найден'), show_alert=True
        )
        return

    code = promo.code
    success = await delete_promocode(db, promo)

    if success:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_073', '✅ Промокод {v0} удален').format(v0=code),
            show_alert=True,
        )
        await show_promocodes_list(callback, db_user, db)
    else:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_074', '❌ Ошибка удаления промокода'), show_alert=True
        )


@admin_required
@error_handler
async def show_promocode_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    promo_id = int(callback.data.split('_')[-1])

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_062', '❌ Промокод не найден'), show_alert=True
        )
        return

    stats = await get_promocode_statistics(db, promo_id)

    text = (
        get_texts(db_user.language)
        .t(
            'ADMIN_PROMOCODE_UI_013',
            '\n📊 <b>Статистика промокода</b> <code>{v0}</code>\n\n📈 <b>Общая статистика:</b>\n- Всего использований: {v1}\n- Использований сегодня: {v2}\n- Осталось использований: {v3}\n\n📅 <b>Последние использования:</b>\n',
        )
        .format(v0=promo.code, v1=stats['total_uses'], v2=stats['today_uses'], v3=promo.max_uses - promo.current_uses)
    )

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
        text += get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_037', '- Пока не было использований\n')

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_102', '⬅️ Назад'),
                    callback_data=f'promo_manage_{promo.id}',
                )
            ]
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def show_general_promocode_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    total_codes = await get_promocodes_count(db)
    active_codes = await get_promocodes_count(db, is_active=True)

    text = (
        get_texts(db_user.language)
        .t(
            'ADMIN_PROMOCODE_UI_014',
            '\n📊 <b>Общая статистика промокодов</b>\n\n📈 <b>Основные показатели:</b>\n- Всего промокодов: {v0}\n- Активных: {v1}\n- Неактивных: {v2}\n\nДля детальной статистики выберите конкретный промокод из списка.\n',
        )
        .format(v0=total_codes, v1=active_codes, v2=total_codes - active_codes)
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_115', '🎫 К промокодам'),
                    callback_data='admin_promo_list',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_PROMOCODE_UI_102', '⬅️ Назад'),
                    callback_data='admin_promocodes',
                )
            ],
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
    dp.message.register(process_promocode_combo_days, AdminStates.setting_promocode_combo_days)
    dp.message.register(process_promocode_uses, AdminStates.setting_promocode_uses)
    dp.message.register(process_promocode_expiry, AdminStates.setting_promocode_expiry)
    dp.message.register(process_discount_hours, AdminStates.setting_discount_hours)
