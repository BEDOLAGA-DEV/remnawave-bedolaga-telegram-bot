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


_VALIDATION_ERROR_KEYS = {
    'не указана цена за 1 день': 'ADMIN_TARIFF_VALIDATION_DAYS_PRICE_MISSING',
    'цена за 1 день должна быть больше нуля': 'ADMIN_TARIFF_VALIDATION_DAYS_PRICE_POSITIVE',
    'не указан минимум дней': 'ADMIN_TARIFF_VALIDATION_DAYS_MIN_MISSING',
    'минимум дней должен быть больше нуля': 'ADMIN_TARIFF_VALIDATION_DAYS_MIN_POSITIVE',
    'не указан максимум дней': 'ADMIN_TARIFF_VALIDATION_DAYS_MAX_MISSING',
    'максимум дней должен быть больше нуля': 'ADMIN_TARIFF_VALIDATION_DAYS_MAX_POSITIVE',
    'максимум дней не может быть меньше минимума': 'ADMIN_TARIFF_VALIDATION_DAYS_RANGE',
}


def _localized_validation_error(error: str, language: str) -> str:
    # Closed error vocabulary from the tariff validator; unknown errors retain their original text.
    key = _VALIDATION_ERROR_KEYS.get(error)
    return get_texts(language).t(key, error) if key else error


def _days(value: int | None, language: str = 'ru') -> str:
    return (
        get_texts(language).t('ADMIN_TARIFF_HELPER_DAYS_1', '{v0} дн.').format(v0=value)
        if value is not None and value > 0
        else get_texts(language).t('ADMIN_TARIFF_HELPER_UNSET', 'Не задано')
    )


def _price(value: int | None, language: str = 'ru') -> str:
    return (
        format_price_kopeks(value)
        if value is not None and value > 0
        else get_texts(language).t('ADMIN_TARIFF_HELPER_UNSET', 'Не задано')
    )


def format_custom_days_settings(tariff: Tariff, language: str = 'ru') -> str:
    """Блок для карточки тарифа."""
    enabled = getattr(tariff, 'custom_days_enabled', False)
    status = (
        get_texts(language).t('ADMIN_TARIFF_HELPER_ENABLED_STATE', '✅ Включено')
        if enabled
        else get_texts(language).t('ADMIN_TARIFF_HELPER_DISABLED_STATE', '❌ Выключено')
    )
    return (
        get_texts(language)
        .t(
            'ADMIN_TARIFF_HELPER_FORMAT_CUSTOM_DAYS_SETTINGS_1',
            '{v0}\n• Цена за 1 день: {v1}\n• Минимум: {v2}\n• Максимум: {v3}',
        )
        .format(
            v0=status,
            v1=_price(getattr(tariff, 'price_per_day_kopeks', None), language=language),
            v2=_days(getattr(tariff, 'min_days', None), language=language),
            v3=_days(getattr(tariff, 'max_days', None), language=language),
        )
    )


def render_custom_days_settings(tariff: Tariff, language: str = 'ru') -> str:
    """Отдельный экран настроек."""
    enabled = getattr(tariff, 'custom_days_enabled', False)
    status = (
        get_texts(language).t('ADMIN_TARIFF_HELPER_ENABLED_MODE', '✅ Включён')
        if enabled
        else get_texts(language).t('ADMIN_TARIFF_HELPER_DISABLED_MODE', '❌ Выключен')
    )
    return (
        get_texts(language)
        .t(
            'ADMIN_TARIFF_HELPER_RENDER_CUSTOM_DAYS_SETTINGS_1',
            '📅 <b>Произвольное количество дней</b>\n\nТариф: <b>{v0}</b>\n\nСтатус: {v1}\nЦена за 1 день: <b>{v2}</b>\nМинимум: <b>{v3}</b>\nМаксимум: <b>{v4}</b>\n\nПользователь сможет выбрать срок в указанных границах; цена — дни × цена за день.',
        )
        .format(
            v0=html.escape(tariff.name),
            v1=status,
            v2=_price(getattr(tariff, 'price_per_day_kopeks', None), language=language),
            v3=_days(getattr(tariff, 'min_days', None), language=language),
            v4=_days(getattr(tariff, 'max_days', None), language=language),
        )
    )


def get_custom_days_keyboard(tariff: Tariff, language: str) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    enabled = getattr(tariff, 'custom_days_enabled', False)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=get_texts(language).t('ADMIN_TARIFF_HELPER_DISABLE_BUTTON', '❌ Выключить')
                    if enabled
                    else get_texts(language).t('ADMIN_TARIFF_HELPER_ENABLE_BUTTON', '✅ Включить'),
                    callback_data=f'admin_tariff_toggle_custom_days:{tariff.id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=get_texts(language).t('ADMIN_TARIFF_HELPER_DAY_PRICE_BUTTON', '💰 Цена за 1 день'),
                    callback_data=f'admin_tariff_edit_custom_days_price:{tariff.id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=get_texts(language).t('ADMIN_TARIFF_HELPER_MIN_DAYS_BUTTON', '📉 Минимум дней'),
                    callback_data=f'admin_tariff_edit_custom_days_min:{tariff.id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=get_texts(language).t('ADMIN_TARIFF_HELPER_MAX_DAYS_BUTTON', '📈 Максимум дней'),
                    callback_data=f'admin_tariff_edit_custom_days_max:{tariff.id}',
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
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return
    await callback.message.edit_text(
        render_custom_days_settings(tariff, language=db_user.language),
        reply_markup=get_custom_days_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_custom_days(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    if getattr(tariff, 'custom_days_enabled', False):
        tariff = await update_tariff(db, tariff, custom_days_enabled=False)
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_HELPER_DAYS_DISABLED', 'Произвольные дни выключены')
        )
    else:
        errors = validate_custom_days_configuration(
            price_per_day_kopeks=getattr(tariff, 'price_per_day_kopeks', None),
            min_days=getattr(tariff, 'min_days', None),
            max_days=getattr(tariff, 'max_days', None),
        )
        if errors:
            details = '\n'.join(f'• {_localized_validation_error(error, db_user.language)}' for error in errors)
            await callback.answer(
                get_texts(db_user.language)
                .t('ADMIN_TARIFF_HELPER_TOGGLE_CUSTOM_DAYS_1', 'Нельзя включить произвольные дни:\n{v0}')
                .format(v0=details),
                show_alert=True,
            )
            return
        tariff = await update_tariff(db, tariff, custom_days_enabled=True)
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_HELPER_DAYS_ENABLED', 'Произвольные дни включены')
        )

    await callback.message.edit_text(
        render_custom_days_settings(tariff, language=db_user.language),
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
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_HELPER_START_FIELD_EDIT_1',
            '{v0}\n\nТариф: <b>{v1}</b>\nТекущее значение: <b>{v2}</b>\n\n{v3}',
        )
        .format(v0=title, v1=html.escape(tariff.name), v2=current_value, v3=prompt),
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
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return
    await _start_field_edit(
        callback,
        db_user,
        state,
        tariff,
        state_value=AdminStates.editing_tariff_custom_days_price,
        title=get_texts(db_user.language).t('ADMIN_TARIFF_HELPER_DAY_PRICE_TITLE', '💰 <b>Цена за 1 день</b>'),
        current_value=_price(getattr(tariff, 'price_per_day_kopeks', None), language=db_user.language),
        prompt=get_texts(db_user.language).t(
            'ADMIN_TARIFF_HELPER_DAY_PRICE_PROMPT',
            'Введите цену за 1 день в рублях.\nПример: <code>15</code> или <code>12.50</code>',
        ),
    )


@admin_required
@error_handler
async def start_edit_custom_days_min(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return
    await _start_field_edit(
        callback,
        db_user,
        state,
        tariff,
        state_value=AdminStates.editing_tariff_custom_days_min,
        title=get_texts(db_user.language).t('ADMIN_TARIFF_HELPER_MIN_DAYS_TITLE', '📉 <b>Минимум дней</b>'),
        current_value=_days(getattr(tariff, 'min_days', None), language=db_user.language),
        prompt=get_texts(db_user.language).t(
            'ADMIN_TARIFF_HELPER_MIN_DAYS_PROMPT', 'Введите минимальный срок целым числом дней.\nПример: <code>3</code>'
        ),
    )


@admin_required
@error_handler
async def start_edit_custom_days_max(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return
    await _start_field_edit(
        callback,
        db_user,
        state,
        tariff,
        state_value=AdminStates.editing_tariff_custom_days_max,
        title=get_texts(db_user.language).t('ADMIN_TARIFF_HELPER_MAX_DAYS_TITLE', '📈 <b>Максимум дней</b>'),
        current_value=_days(getattr(tariff, 'max_days', None), language=db_user.language),
        prompt=get_texts(db_user.language).t(
            'ADMIN_TARIFF_HELPER_MAX_DAYS_PROMPT',
            'Введите максимальный срок целым числом дней.\nПример: <code>90</code>',
        ),
    )


async def _load_tariff_from_state(
    message: types.Message, db: AsyncSession, state: FSMContext, language: str = 'ru'
) -> Tariff | None:
    tariff_id = (await state.get_data()).get('tariff_id')
    tariff = await get_tariff_by_id(db, tariff_id) if tariff_id is not None else None
    if tariff is None:
        await message.answer(get_texts(language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
        await state.clear()
    return tariff


async def _finish(message: types.Message, db_user: User, state: FSMContext, tariff: Tariff, confirmation: str) -> None:
    await state.clear()
    await message.answer(
        f'{confirmation}\n\n{render_custom_days_settings(tariff, language=db_user.language)}',
        reply_markup=get_custom_days_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def process_custom_days_price_input(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await _load_tariff_from_state(message, db, state, language=db_user.language)
    if tariff is None:
        return
    try:
        price_kopeks = parse_positive_rubles_to_kopeks(message.text or '')
    except ValueError:
        await message.answer(
            get_texts(db_user.language).t(
                'ADMIN_TARIFF_HELPER_INVALID_DAY_PRICE',
                '❌ Некорректная цена. Введите положительную сумму в рублях с точностью не более двух знаков.\nПример: <code>15</code> или <code>12.50</code>',
            ),
            parse_mode='HTML',
        )
        return
    tariff = await update_tariff(db, tariff, price_per_day_kopeks=price_kopeks)
    await _finish(
        message,
        db_user,
        state,
        tariff,
        get_texts(db_user.language)
        .t('ADMIN_TARIFF_HELPER_PROCESS_CUSTOM_DAYS_PRICE_INPUT_1', '✅ Цена за 1 день установлена: {v0}')
        .format(v0=format_price_kopeks(price_kopeks)),
    )


@admin_required
@error_handler
async def process_custom_days_min_input(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await _load_tariff_from_state(message, db, state, language=db_user.language)
    if tariff is None:
        return
    try:
        minimum = parse_positive_days(message.text or '')
    except ValueError:
        await message.answer(
            get_texts(db_user.language).t(
                'ADMIN_TARIFF_HELPER_INVALID_MIN_DAYS',
                '❌ Введите положительное целое число дней.\nПример: <code>3</code>',
            ),
            parse_mode='HTML',
        )
        return
    maximum = getattr(tariff, 'max_days', None)
    if maximum is not None and maximum > 0 and minimum > maximum:
        await message.answer(
            get_texts(db_user.language)
            .t(
                'ADMIN_TARIFF_HELPER_PROCESS_CUSTOM_DAYS_MIN_INPUT_2',
                '❌ Минимум дней не может быть больше текущего максимума ({v0} дн.).',
            )
            .format(v0=maximum)
        )
        return
    tariff = await update_tariff(db, tariff, min_days=minimum)
    await _finish(
        message,
        db_user,
        state,
        tariff,
        get_texts(db_user.language)
        .t('ADMIN_TARIFF_HELPER_PROCESS_CUSTOM_DAYS_MIN_INPUT_1', '✅ Минимум установлен: {v0} дн.')
        .format(v0=minimum),
    )


@admin_required
@error_handler
async def process_custom_days_max_input(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await _load_tariff_from_state(message, db, state, language=db_user.language)
    if tariff is None:
        return
    try:
        maximum = parse_positive_days(message.text or '')
    except ValueError:
        await message.answer(
            get_texts(db_user.language).t(
                'ADMIN_TARIFF_HELPER_INVALID_MAX_DAYS',
                '❌ Введите положительное целое число дней.\nПример: <code>90</code>',
            ),
            parse_mode='HTML',
        )
        return
    minimum = getattr(tariff, 'min_days', None)
    if minimum is not None and minimum > 0 and maximum < minimum:
        await message.answer(
            get_texts(db_user.language)
            .t(
                'ADMIN_TARIFF_HELPER_PROCESS_CUSTOM_DAYS_MAX_INPUT_2',
                '❌ Максимум дней не может быть меньше текущего минимума ({v0} дн.).',
            )
            .format(v0=minimum)
        )
        return
    tariff = await update_tariff(db, tariff, max_days=maximum)
    await _finish(
        message,
        db_user,
        state,
        tariff,
        get_texts(db_user.language)
        .t('ADMIN_TARIFF_HELPER_PROCESS_CUSTOM_DAYS_MAX_INPUT_1', '✅ Максимум установлен: {v0} дн.')
        .format(v0=maximum),
    )


def register_custom_days_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_custom_days_settings, F.data.startswith('admin_tariff_edit_custom_days:'))
    dp.callback_query.register(toggle_custom_days, F.data.startswith('admin_tariff_toggle_custom_days:'))
    dp.callback_query.register(start_edit_custom_days_price, F.data.startswith('admin_tariff_edit_custom_days_price:'))
    dp.callback_query.register(start_edit_custom_days_min, F.data.startswith('admin_tariff_edit_custom_days_min:'))
    dp.callback_query.register(start_edit_custom_days_max, F.data.startswith('admin_tariff_edit_custom_days_max:'))
    dp.message.register(process_custom_days_price_input, AdminStates.editing_tariff_custom_days_price)
    dp.message.register(process_custom_days_min_input, AdminStates.editing_tariff_custom_days_min)
    dp.message.register(process_custom_days_max_input, AdminStates.editing_tariff_custom_days_max)
