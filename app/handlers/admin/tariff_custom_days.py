"""Телеграм-редактор: произвольное количество дней у тарифа (как произвольный трафик)."""

import html

from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.tariff import get_tariff_by_id, update_tariff
from app.database.models import Tariff, User
from app.localization.texts import get_texts
from app.services.tariff_custom_days import parse_positive_days, validate_custom_days_configuration
from app.services.tariff_custom_traffic import parse_positive_rubles_to_kopeks
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler
from app.utils.formatting import format_price_kopeks


def _days(value: int | None) -> str:
    return f'{value} дн.' if value is not None and value > 0 else 'Не задано'


def _price(value: int | None) -> str:
    return format_price_kopeks(value) if value is not None and value > 0 else 'Не задано'


def format_custom_days_settings(tariff: Tariff) -> str:
    """Блок для карточки тарифа."""
    enabled = getattr(tariff, 'custom_days_enabled', False)
    status = '✅ Включено' if enabled else '❌ Выключено'
    return (
        f'{status}\n'
        f'• Цена за 1 день: {_price(getattr(tariff, "price_per_day_kopeks", None))}\n'
        f'• Минимум: {_days(getattr(tariff, "min_days", None))}\n'
        f'• Максимум: {_days(getattr(tariff, "max_days", None))}'
    )


def render_custom_days_settings(tariff: Tariff) -> str:
    """Отдельный экран настроек."""
    enabled = getattr(tariff, 'custom_days_enabled', False)
    status = '✅ Включён' if enabled else '❌ Выключен'
    return (
        f'📅 <b>Произвольное количество дней</b>\n\n'
        f'Тариф: <b>{html.escape(tariff.name)}</b>\n\n'
        f'Статус: {status}\n'
        f'Цена за 1 день: <b>{_price(getattr(tariff, "price_per_day_kopeks", None))}</b>\n'
        f'Минимум: <b>{_days(getattr(tariff, "min_days", None))}</b>\n'
        f'Максимум: <b>{_days(getattr(tariff, "max_days", None))}</b>\n\n'
        'Пользователь сможет выбрать срок в указанных границах; цена — дни × цена за день.'
    )


def get_custom_days_keyboard(tariff: Tariff, language: str) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    enabled = getattr(tariff, 'custom_days_enabled', False)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text='❌ Выключить' if enabled else '✅ Включить',
                    callback_data=f'admin_tariff_toggle_custom_days:{tariff.id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text='💰 Цена за 1 день', callback_data=f'admin_tariff_edit_custom_days_price:{tariff.id}'
                )
            ],
            [
                InlineKeyboardButton(
                    text='📉 Минимум дней', callback_data=f'admin_tariff_edit_custom_days_min:{tariff.id}'
                )
            ],
            [
                InlineKeyboardButton(
                    text='📈 Максимум дней', callback_data=f'admin_tariff_edit_custom_days_max:{tariff.id}'
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff.id}')],
        ]
    )


@admin_required
@error_handler
async def show_custom_days_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.clear()
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return
    await callback.message.edit_text(
        render_custom_days_settings(tariff),
        reply_markup=get_custom_days_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_custom_days(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return

    if getattr(tariff, 'custom_days_enabled', False):
        tariff = await update_tariff(db, tariff, custom_days_enabled=False)
        await callback.answer('Произвольные дни выключены')
    else:
        errors = validate_custom_days_configuration(
            price_per_day_kopeks=getattr(tariff, 'price_per_day_kopeks', None),
            min_days=getattr(tariff, 'min_days', None),
            max_days=getattr(tariff, 'max_days', None),
        )
        if errors:
            details = '\n'.join(f'• {error}' for error in errors)
            await callback.answer(f'Нельзя включить произвольные дни:\n{details}', show_alert=True)
            return
        tariff = await update_tariff(db, tariff, custom_days_enabled=True)
        await callback.answer('Произвольные дни включены')

    await callback.message.edit_text(
        render_custom_days_settings(tariff),
        reply_markup=get_custom_days_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


async def _start_field_edit(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    tariff: Tariff,
    *,
    state_value: State,
    title: str,
    current_value: str,
    prompt: str,
) -> None:
    await state.set_state(state_value)
    await state.update_data(tariff_id=tariff.id, language=db_user.language)
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        f'{title}\n\nТариф: <b>{html.escape(tariff.name)}</b>\nТекущее значение: <b>{current_value}</b>\n\n{prompt}',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_edit_custom_days:{tariff.id}')]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def start_edit_custom_days_price(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return
    await _start_field_edit(
        callback,
        db_user,
        state,
        tariff,
        state_value=AdminStates.editing_tariff_custom_days_price,
        title='💰 <b>Цена за 1 день</b>',
        current_value=_price(getattr(tariff, 'price_per_day_kopeks', None)),
        prompt='Введите цену за 1 день в рублях.\nПример: <code>15</code> или <code>12.50</code>',
    )


@admin_required
@error_handler
async def start_edit_custom_days_min(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return
    await _start_field_edit(
        callback,
        db_user,
        state,
        tariff,
        state_value=AdminStates.editing_tariff_custom_days_min,
        title='📉 <b>Минимум дней</b>',
        current_value=_days(getattr(tariff, 'min_days', None)),
        prompt='Введите минимальный срок целым числом дней.\nПример: <code>3</code>',
    )


@admin_required
@error_handler
async def start_edit_custom_days_max(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return
    await _start_field_edit(
        callback,
        db_user,
        state,
        tariff,
        state_value=AdminStates.editing_tariff_custom_days_max,
        title='📈 <b>Максимум дней</b>',
        current_value=_days(getattr(tariff, 'max_days', None)),
        prompt='Введите максимальный срок целым числом дней.\nПример: <code>90</code>',
    )


async def _load_tariff_from_state(message: types.Message, db: AsyncSession, state: FSMContext) -> Tariff | None:
    tariff_id = (await state.get_data()).get('tariff_id')
    tariff = await get_tariff_by_id(db, tariff_id) if tariff_id is not None else None
    if tariff is None:
        await message.answer('Тариф не найден')
        await state.clear()
    return tariff


async def _finish(message: types.Message, db_user: User, state: FSMContext, tariff: Tariff, confirmation: str) -> None:
    await state.clear()
    await message.answer(
        f'{confirmation}\n\n{render_custom_days_settings(tariff)}',
        reply_markup=get_custom_days_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def process_custom_days_price_input(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await _load_tariff_from_state(message, db, state)
    if tariff is None:
        return
    try:
        price_kopeks = parse_positive_rubles_to_kopeks(message.text or '')
    except ValueError:
        await message.answer(
            '❌ Некорректная цена. Введите положительную сумму в рублях с точностью не более двух знаков.\n'
            'Пример: <code>15</code> или <code>12.50</code>',
            parse_mode='HTML',
        )
        return
    tariff = await update_tariff(db, tariff, price_per_day_kopeks=price_kopeks)
    await _finish(
        message, db_user, state, tariff, f'✅ Цена за 1 день установлена: {format_price_kopeks(price_kopeks)}'
    )


@admin_required
@error_handler
async def process_custom_days_min_input(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await _load_tariff_from_state(message, db, state)
    if tariff is None:
        return
    try:
        minimum = parse_positive_days(message.text or '')
    except ValueError:
        await message.answer('❌ Введите положительное целое число дней.\nПример: <code>3</code>', parse_mode='HTML')
        return
    maximum = getattr(tariff, 'max_days', None)
    if maximum is not None and maximum > 0 and minimum > maximum:
        await message.answer(f'❌ Минимум дней не может быть больше текущего максимума ({maximum} дн.).')
        return
    tariff = await update_tariff(db, tariff, min_days=minimum)
    await _finish(message, db_user, state, tariff, f'✅ Минимум установлен: {minimum} дн.')


@admin_required
@error_handler
async def process_custom_days_max_input(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await _load_tariff_from_state(message, db, state)
    if tariff is None:
        return
    try:
        maximum = parse_positive_days(message.text or '')
    except ValueError:
        await message.answer('❌ Введите положительное целое число дней.\nПример: <code>90</code>', parse_mode='HTML')
        return
    minimum = getattr(tariff, 'min_days', None)
    if minimum is not None and minimum > 0 and maximum < minimum:
        await message.answer(f'❌ Максимум дней не может быть меньше текущего минимума ({minimum} дн.).')
        return
    tariff = await update_tariff(db, tariff, max_days=maximum)
    await _finish(message, db_user, state, tariff, f'✅ Максимум установлен: {maximum} дн.')


def register_custom_days_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_custom_days_settings, F.data.startswith('admin_tariff_edit_custom_days:'))
    dp.callback_query.register(toggle_custom_days, F.data.startswith('admin_tariff_toggle_custom_days:'))
    dp.callback_query.register(start_edit_custom_days_price, F.data.startswith('admin_tariff_edit_custom_days_price:'))
    dp.callback_query.register(start_edit_custom_days_min, F.data.startswith('admin_tariff_edit_custom_days_min:'))
    dp.callback_query.register(start_edit_custom_days_max, F.data.startswith('admin_tariff_edit_custom_days_max:'))
    dp.message.register(process_custom_days_price_input, AdminStates.editing_tariff_custom_days_price)
    dp.message.register(process_custom_days_min_input, AdminStates.editing_tariff_custom_days_min)
    dp.message.register(process_custom_days_max_input, AdminStates.editing_tariff_custom_days_max)
