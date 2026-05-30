"""Admin in-bot panel for the birthday-bonus feature.

Buttons-driven editor for BirthdaySettingsService (the singleton JSON-backed
store that controls enabled/reward_type/reward_amount/promocode_valid_days/
min_account_age_days/dob_stable_days/subscription_days_fallback knobs).

Callback prefix: ``admin_birthday_``.
"""

from __future__ import annotations

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.birthday_settings_service import BirthdaySettingsService
from app.states import BirthdayAdminStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


# ---------------- Keyboards ----------------


def _config_keyboard(cfg: dict) -> InlineKeyboardMarkup:
    on = '🟢' if cfg.get('enabled') else '🔴'
    reward_type = cfg.get('reward_type', 'balance')
    fallback = cfg.get('subscription_days_fallback', 'balance')
    rows = [
        [InlineKeyboardButton(
            text=f'{on} Включено: {"Да" if cfg.get("enabled") else "Нет"}',
            callback_data='admin_birthday_toggle',
        )],
        [InlineKeyboardButton(
            text=f'🎁 Тип награды: {reward_type}',
            callback_data='admin_birthday_cycle_type',
        )],
        [InlineKeyboardButton(
            text=f'💰 Размер награды: {_format_amount(cfg)}',
            callback_data='admin_birthday_edit_amount',
        )],
        [InlineKeyboardButton(
            text=f'📅 Срок промокода: {cfg.get("promocode_valid_days", 7)} дн.',
            callback_data='admin_birthday_edit_promodays',
        )],
        [InlineKeyboardButton(
            text=f'👤 Мин. возраст аккаунта: {cfg.get("min_account_age_days", 7)} дн.',
            callback_data='admin_birthday_edit_minage',
        )],
        [InlineKeyboardButton(
            text=f'🔒 Стабильность ДР: {cfg.get("dob_stable_days", 7)} дн.',
            callback_data='admin_birthday_edit_dobstable',
        )],
        [InlineKeyboardButton(
            text=f'🔀 Fallback (subscription_days→): {fallback}',
            callback_data='admin_birthday_cycle_fallback',
        )],
        [InlineKeyboardButton(text='⬅ В настройки', callback_data='admin_submenu_settings')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_amount(cfg: dict) -> str:
    reward_type = cfg.get('reward_type', 'balance')
    amount = cfg.get('reward_amount', 0)
    if reward_type == 'balance':
        rubles = amount // 100
        kopeks = amount % 100
        if kopeks:
            return f'{rubles}.{kopeks:02d} ₽ ({amount} коп.)'
        return f'{rubles} ₽'
    elif reward_type == 'subscription_days':
        return f'{amount} дн.'
    elif reward_type == 'promocode':
        return f'{amount}%'
    return str(amount)


# ---------------- Render helper ----------------


def _format_config(cfg: dict) -> str:
    fallback = cfg.get('subscription_days_fallback', 'balance')
    return (
        '🎂 <b>Настройки подарка на день рождения</b>\n\n'
        f'Включено: <b>{"Да" if cfg.get("enabled") else "Нет"}</b>\n'
        f'Тип награды: <b>{cfg.get("reward_type", "balance")}</b>\n'
        f'Размер награды: <b>{_format_amount(cfg)}</b>\n'
        f'Срок промокода: <b>{cfg.get("promocode_valid_days", 7)} дн.</b>\n'
        f'Мин. возраст аккаунта: <b>{cfg.get("min_account_age_days", 7)} дн.</b>\n'
        f'Стабильность ДР (dob_stable_days): <b>{cfg.get("dob_stable_days", 7)} дн.</b>\n'
        f'Fallback при subscription_days: <b>{fallback}</b>\n\n'
        f'<i>Типы награды: balance (баланс, копейки) | subscription_days (дни) | promocode (% скидки)</i>\n'
        f'<i>Fallback: balance — начислить баланс если нет активной подписки; skip — пропустить</i>'
    )


# ---------------- Top-level entry ----------------


@admin_required
@error_handler
async def open_panel(callback: types.CallbackQuery, **kwargs):
    cfg = BirthdaySettingsService.get_config()
    await callback.message.edit_text(_format_config(cfg), parse_mode='HTML', reply_markup=_config_keyboard(cfg))
    await callback.answer()


# ---------------- Toggle enabled ----------------


@admin_required
@error_handler
async def toggle_enabled(callback: types.CallbackQuery, **kwargs):
    BirthdaySettingsService.set_enabled(not BirthdaySettingsService.is_enabled())
    cfg = BirthdaySettingsService.get_config()
    await callback.message.edit_text(_format_config(cfg), parse_mode='HTML', reply_markup=_config_keyboard(cfg))
    await callback.answer('✅ Переключено')


# ---------------- Cycle reward_type ----------------


_REWARD_TYPE_CYCLE = ['balance', 'subscription_days', 'promocode']


@admin_required
@error_handler
async def cycle_reward_type(callback: types.CallbackQuery, **kwargs):
    current = BirthdaySettingsService.get_reward_type()
    try:
        idx = _REWARD_TYPE_CYCLE.index(current)
    except ValueError:
        idx = 0
    next_type = _REWARD_TYPE_CYCLE[(idx + 1) % len(_REWARD_TYPE_CYCLE)]
    BirthdaySettingsService.set_reward_type(next_type)
    cfg = BirthdaySettingsService.get_config()
    await callback.message.edit_text(_format_config(cfg), parse_mode='HTML', reply_markup=_config_keyboard(cfg))
    await callback.answer(f'✅ Тип награды: {next_type}')


# ---------------- Cycle subscription_days_fallback ----------------


_FALLBACK_CYCLE = ['balance', 'skip']


@admin_required
@error_handler
async def cycle_fallback(callback: types.CallbackQuery, **kwargs):
    current = BirthdaySettingsService.get_subscription_days_fallback()
    try:
        idx = _FALLBACK_CYCLE.index(current)
    except ValueError:
        idx = 0
    next_fallback = _FALLBACK_CYCLE[(idx + 1) % len(_FALLBACK_CYCLE)]
    BirthdaySettingsService.set_subscription_days_fallback(next_fallback)
    cfg = BirthdaySettingsService.get_config()
    await callback.message.edit_text(_format_config(cfg), parse_mode='HTML', reply_markup=_config_keyboard(cfg))
    await callback.answer(f'✅ Fallback: {next_fallback}')


# ---------------- Numeric edits via FSM ----------------


@admin_required
@error_handler
async def edit_amount(callback: types.CallbackQuery, state: FSMContext, **kwargs):
    reward_type = BirthdaySettingsService.get_reward_type()
    if reward_type == 'balance':
        hint = '(копейки, ≥0; 1 ₽ = 100 коп.)'
    elif reward_type == 'subscription_days':
        hint = '(дней, ≥0)'
    else:
        hint = '(процент скидки, ≥0)'
    await state.set_state(BirthdayAdminStates.waiting_for_amount)
    await callback.message.answer(
        f'💰 Введите размер награды {hint}\n\nОтправьте число сообщением или /cancel.'
    )
    await callback.answer()


@admin_required
@error_handler
async def edit_promodays(callback: types.CallbackQuery, state: FSMContext, **kwargs):
    await state.set_state(BirthdayAdminStates.waiting_for_promodays)
    await callback.message.answer(
        '📅 Введите срок действия промокода в днях (1–365):\n\nОтправьте число сообщением или /cancel.'
    )
    await callback.answer()


@admin_required
@error_handler
async def edit_minage(callback: types.CallbackQuery, state: FSMContext, **kwargs):
    await state.set_state(BirthdayAdminStates.waiting_for_minage)
    await callback.message.answer(
        '👤 Введите минимальный возраст аккаунта в днях (0–365):\n\nОтправьте число сообщением или /cancel.'
    )
    await callback.answer()


@admin_required
@error_handler
async def edit_dobstable(callback: types.CallbackQuery, state: FSMContext, **kwargs):
    await state.set_state(BirthdayAdminStates.waiting_for_dobstable)
    await callback.message.answer(
        '🔒 Введите количество дней стабильности ДР dob_stable_days (0–365):\n\nОтправьте число сообщением или /cancel.'
    )
    await callback.answer()


# ---------------- FSM message handler ----------------


async def _handle_numeric_input(message: types.Message, state: FSMContext):
    raw = (message.text or '').strip()
    if raw.lower() in ('/cancel', 'cancel', 'отмена'):
        await state.clear()
        await message.answer('Отменено.')
        return

    current_state = await state.get_state()

    try:
        value = int(raw)
    except ValueError:
        await message.answer('❌ Не число. Повторите или /cancel.')
        return

    if current_state == BirthdayAdminStates.waiting_for_amount:
        if value < 0:
            await message.answer('❌ Значение должно быть ≥ 0. Повторите или /cancel.')
            return
        result = BirthdaySettingsService.set_reward_amount(value)
        if result is False:
            await message.answer('❌ Не удалось сохранить. Повторите или /cancel.')
            return

    elif current_state == BirthdayAdminStates.waiting_for_promodays:
        if value < 1 or value > 365:
            await message.answer('❌ Вне диапазона [1; 365]. Повторите или /cancel.')
            return
        result = BirthdaySettingsService.set_promocode_valid_days(value)
        if result is False:
            await message.answer('❌ Не удалось сохранить. Повторите или /cancel.')
            return

    elif current_state == BirthdayAdminStates.waiting_for_minage:
        if value < 0 or value > 365:
            await message.answer('❌ Вне диапазона [0; 365]. Повторите или /cancel.')
            return
        result = BirthdaySettingsService.set_min_account_age_days(value)
        if result is False:
            await message.answer('❌ Не удалось сохранить. Повторите или /cancel.')
            return

    elif current_state == BirthdayAdminStates.waiting_for_dobstable:
        if value < 0 or value > 365:
            await message.answer('❌ Вне диапазона [0; 365]. Повторите или /cancel.')
            return
        result = BirthdaySettingsService.set_dob_stable_days(value)
        if result is False:
            await message.answer('❌ Не удалось сохранить. Повторите или /cancel.')
            return

    else:
        await state.clear()
        await message.answer('❌ Неизвестное состояние. Отменено.')
        return

    await state.clear()
    cfg = BirthdaySettingsService.get_config()
    await message.answer(_format_config(cfg), parse_mode='HTML', reply_markup=_config_keyboard(cfg))


# ---------------- Registration ----------------


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(open_panel, F.data == 'admin_birthday_menu')
    dp.callback_query.register(toggle_enabled, F.data == 'admin_birthday_toggle')
    dp.callback_query.register(cycle_reward_type, F.data == 'admin_birthday_cycle_type')
    dp.callback_query.register(cycle_fallback, F.data == 'admin_birthday_cycle_fallback')

    dp.callback_query.register(edit_amount, F.data == 'admin_birthday_edit_amount')
    dp.callback_query.register(edit_promodays, F.data == 'admin_birthday_edit_promodays')
    dp.callback_query.register(edit_minage, F.data == 'admin_birthday_edit_minage')
    dp.callback_query.register(edit_dobstable, F.data == 'admin_birthday_edit_dobstable')

    # FSM message handlers — gated on state
    dp.message.register(_handle_numeric_input, BirthdayAdminStates.waiting_for_amount)
    dp.message.register(_handle_numeric_input, BirthdayAdminStates.waiting_for_promodays)
    dp.message.register(_handle_numeric_input, BirthdayAdminStates.waiting_for_minage)
    dp.message.register(_handle_numeric_input, BirthdayAdminStates.waiting_for_dobstable)
