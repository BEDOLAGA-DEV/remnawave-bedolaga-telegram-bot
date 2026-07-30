"""Telegram administrator UI for custom-traffic tariff fields."""

import html

from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.tariff import get_tariff_by_id, update_tariff
from app.database.models import Tariff, User
from app.localization.texts import get_texts
from app.services.tariff_custom_traffic import (
    parse_positive_gb,
    parse_positive_rubles_to_kopeks,
    validate_custom_traffic_configuration,
)
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler
from app.utils.formatting import format_price_kopeks


def _format_custom_traffic_value(value: int | None, suffix: str = '') -> str:
    """Format an optional positive custom-traffic value for the admin UI."""
    if value is None or value <= 0:
        return 'Не задано'
    return f'{value}{suffix}'


def format_custom_traffic_settings(tariff: Tariff) -> str:
    """Format custom-traffic state for the main tariff card."""
    enabled = getattr(tariff, 'custom_traffic_enabled', False)
    price = getattr(tariff, 'traffic_price_per_gb_kopeks', None)
    minimum = getattr(tariff, 'min_traffic_gb', None)
    maximum = getattr(tariff, 'max_traffic_gb', None)

    status = '✅ Включено' if enabled else '❌ Выключено'
    price_display = format_price_kopeks(price) if price is not None and price > 0 else 'Не задано'
    minimum_display = _format_custom_traffic_value(minimum, ' ГБ')
    maximum_display = _format_custom_traffic_value(maximum, ' ГБ')

    return (
        f'{status}\n'
        f'• Цена за 1 ГБ: {price_display}\n'
        f'• Минимальный объём: {minimum_display}\n'
        f'• Максимальный объём: {maximum_display}'
    )


def render_custom_traffic_settings(tariff: Tariff) -> str:
    """Render the dedicated custom-traffic settings screen."""
    enabled = getattr(tariff, 'custom_traffic_enabled', False)
    price = getattr(tariff, 'traffic_price_per_gb_kopeks', None)
    minimum = getattr(tariff, 'min_traffic_gb', None)
    maximum = getattr(tariff, 'max_traffic_gb', None)

    status = '✅ Включён' if enabled else '❌ Выключен'
    price_display = format_price_kopeks(price) if price is not None and price > 0 else 'Не задано'
    minimum_display = _format_custom_traffic_value(minimum, ' ГБ')
    maximum_display = _format_custom_traffic_value(maximum, ' ГБ')

    return (
        f'⚙️ <b>Произвольный трафик</b>\n\n'
        f'Тариф: <b>{html.escape(tariff.name)}</b>\n\n'
        f'Статус: {status}\n'
        f'Цена за 1 ГБ: <b>{price_display}</b>\n'
        f'Минимальный объём: <b>{minimum_display}</b>\n'
        f'Максимальный объём: <b>{maximum_display}</b>\n\n'
        'Пользователь сможет выбрать объём трафика в указанных границах. '
        'Цена выбранного объёма добавляется к цене периода.'
    )


def get_custom_traffic_keyboard(tariff: Tariff, language: str) -> InlineKeyboardMarkup:
    """Build the dedicated custom-traffic settings keyboard."""
    texts = get_texts(language)
    enabled = getattr(tariff, 'custom_traffic_enabled', False)
    toggle_text = '❌ Выключить' if enabled else '✅ Включить'

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=toggle_text,
                    callback_data=f'admin_tariff_toggle_custom_traffic:{tariff.id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text='💰 Цена за 1 ГБ',
                    callback_data=f'admin_tariff_edit_custom_traffic_price:{tariff.id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text='📉 Минимальный объём',
                    callback_data=f'admin_tariff_edit_custom_traffic_min:{tariff.id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text='📈 Максимальный объём',
                    callback_data=f'admin_tariff_edit_custom_traffic_max:{tariff.id}',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff.id}')],
        ]
    )


@admin_required
@error_handler
async def show_custom_traffic_settings(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Show custom-traffic settings for a tariff and leave any field-edit state."""
    await state.clear()
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return

    await callback.message.edit_text(
        render_custom_traffic_settings(tariff),
        reply_markup=get_custom_traffic_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_custom_traffic(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Enable or disable custom traffic after validating stored settings."""
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return

    if tariff.custom_traffic_enabled:
        tariff = await update_tariff(db, tariff, custom_traffic_enabled=False)
        await callback.answer('Произвольный трафик выключен')
    else:
        errors = validate_custom_traffic_configuration(
            price_per_gb_kopeks=getattr(tariff, 'traffic_price_per_gb_kopeks', None),
            min_traffic_gb=getattr(tariff, 'min_traffic_gb', None),
            max_traffic_gb=getattr(tariff, 'max_traffic_gb', None),
        )
        if errors:
            details = '\n'.join(f'• {error}' for error in errors)
            await callback.answer(
                f'Нельзя включить произвольный трафик:\n{details}',
                show_alert=True,
            )
            return

        tariff = await update_tariff(db, tariff, custom_traffic_enabled=True)
        await callback.answer('Произвольный трафик включён')

    await callback.message.edit_text(
        render_custom_traffic_settings(tariff),
        reply_markup=get_custom_traffic_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


async def _start_custom_traffic_field_edit(
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
    """Start one custom-traffic field editor using the shared FSM contract."""
    tariff_id = tariff.id
    await state.set_state(state_value)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        f'{title}\n\nТариф: <b>{html.escape(tariff.name)}</b>\nТекущее значение: <b>{current_value}</b>\n\n{prompt}',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.CANCEL,
                        callback_data=f'admin_tariff_edit_custom_traffic:{tariff_id}',
                    )
                ]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def start_edit_custom_traffic_price(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Start editing the custom-traffic price per gigabyte."""
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return

    price = getattr(tariff, 'traffic_price_per_gb_kopeks', None)
    current = format_price_kopeks(price) if price is not None and price > 0 else 'Не задано'
    await _start_custom_traffic_field_edit(
        callback,
        db_user,
        state,
        tariff,
        state_value=AdminStates.editing_tariff_custom_traffic_price,
        title='💰 <b>Цена произвольного трафика</b>',
        current_value=current,
        prompt='Введите цену за 1 ГБ в рублях.\nПример: <code>2</code> или <code>2.50</code>',
    )


@admin_required
@error_handler
async def start_edit_custom_traffic_min(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Start editing the minimum selectable traffic amount."""
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return

    current = _format_custom_traffic_value(getattr(tariff, 'min_traffic_gb', None), ' ГБ')
    await _start_custom_traffic_field_edit(
        callback,
        db_user,
        state,
        tariff,
        state_value=AdminStates.editing_tariff_custom_traffic_min,
        title='📉 <b>Минимальный объём</b>',
        current_value=current,
        prompt='Введите минимальный объём целым числом гигабайт.\nПример: <code>5</code>',
    )


@admin_required
@error_handler
async def start_edit_custom_traffic_max(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Start editing the maximum selectable traffic amount."""
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return

    current = _format_custom_traffic_value(getattr(tariff, 'max_traffic_gb', None), ' ГБ')
    await _start_custom_traffic_field_edit(
        callback,
        db_user,
        state,
        tariff,
        state_value=AdminStates.editing_tariff_custom_traffic_max,
        title='📈 <b>Максимальный объём</b>',
        current_value=current,
        prompt='Введите максимальный объём целым числом гигабайт.\nПример: <code>100</code>',
    )


async def _load_custom_traffic_tariff_from_state(
    message: types.Message,
    db: AsyncSession,
    state: FSMContext,
) -> Tariff | None:
    """Resolve the selected tariff and clear stale FSM state if it disappeared."""
    state_data = await state.get_data()
    tariff_id = state_data.get('tariff_id')
    tariff = await get_tariff_by_id(db, tariff_id) if tariff_id is not None else None
    if tariff is None:
        await message.answer('Тариф не найден')
        await state.clear()
    return tariff


async def _send_custom_traffic_update_result(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    tariff: Tariff,
    confirmation: str,
) -> None:
    """Finish a successful field edit and show the refreshed settings screen."""
    await state.clear()
    await message.answer(
        f'{confirmation}\n\n{render_custom_traffic_settings(tariff)}',
        reply_markup=get_custom_traffic_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def process_custom_traffic_price_input(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Persist a validated custom-traffic price per gigabyte."""
    tariff = await _load_custom_traffic_tariff_from_state(message, db, state)
    if tariff is None:
        return

    try:
        price_kopeks = parse_positive_rubles_to_kopeks(message.text or '')
    except ValueError:
        await message.answer(
            '❌ Некорректная цена. Введите положительную сумму в рублях '
            'с точностью не более двух знаков.\nПример: <code>2</code> или <code>2.50</code>',
            parse_mode='HTML',
        )
        return

    tariff = await update_tariff(db, tariff, traffic_price_per_gb_kopeks=price_kopeks)
    await _send_custom_traffic_update_result(
        message,
        db_user,
        state,
        tariff,
        f'✅ Цена за 1 ГБ установлена: {format_price_kopeks(price_kopeks)}',
    )


@admin_required
@error_handler
async def process_custom_traffic_min_input(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Persist a validated minimum selectable traffic amount."""
    tariff = await _load_custom_traffic_tariff_from_state(message, db, state)
    if tariff is None:
        return

    try:
        minimum = parse_positive_gb(message.text or '')
    except ValueError:
        await message.answer(
            '❌ Введите положительное целое число гигабайт.\nПример: <code>5</code>', parse_mode='HTML'
        )
        return

    maximum = getattr(tariff, 'max_traffic_gb', None)
    if maximum is not None and maximum > 0 and minimum > maximum:
        await message.answer(
            f'❌ Минимальный объём не может быть больше текущего максимума ({maximum} ГБ).',
        )
        return

    tariff = await update_tariff(db, tariff, min_traffic_gb=minimum)
    await _send_custom_traffic_update_result(
        message,
        db_user,
        state,
        tariff,
        f'✅ Минимальный объём установлен: {minimum} ГБ',
    )


@admin_required
@error_handler
async def process_custom_traffic_max_input(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Persist a validated maximum selectable traffic amount."""
    tariff = await _load_custom_traffic_tariff_from_state(message, db, state)
    if tariff is None:
        return

    try:
        maximum = parse_positive_gb(message.text or '')
    except ValueError:
        await message.answer(
            '❌ Введите положительное целое число гигабайт.\nПример: <code>100</code>',
            parse_mode='HTML',
        )
        return

    minimum = getattr(tariff, 'min_traffic_gb', None)
    if minimum is not None and minimum > 0 and maximum < minimum:
        await message.answer(
            f'❌ Максимальный объём не может быть меньше текущего минимума ({minimum} ГБ).',
        )
        return

    tariff = await update_tariff(db, tariff, max_traffic_gb=maximum)
    await _send_custom_traffic_update_result(
        message,
        db_user,
        state,
        tariff,
        f'✅ Максимальный объём установлен: {maximum} ГБ',
    )


def register_custom_traffic_handlers(dp: Dispatcher) -> None:
    """Register callbacks and FSM handlers for custom-traffic administration."""
    dp.callback_query.register(show_custom_traffic_settings, F.data.startswith('admin_tariff_edit_custom_traffic:'))
    dp.callback_query.register(toggle_custom_traffic, F.data.startswith('admin_tariff_toggle_custom_traffic:'))
    dp.callback_query.register(
        start_edit_custom_traffic_price, F.data.startswith('admin_tariff_edit_custom_traffic_price:')
    )
    dp.callback_query.register(
        start_edit_custom_traffic_min, F.data.startswith('admin_tariff_edit_custom_traffic_min:')
    )
    dp.callback_query.register(
        start_edit_custom_traffic_max, F.data.startswith('admin_tariff_edit_custom_traffic_max:')
    )
    dp.message.register(process_custom_traffic_price_input, AdminStates.editing_tariff_custom_traffic_price)
    dp.message.register(process_custom_traffic_min_input, AdminStates.editing_tariff_custom_traffic_min)
    dp.message.register(process_custom_traffic_max_input, AdminStates.editing_tariff_custom_traffic_max)
