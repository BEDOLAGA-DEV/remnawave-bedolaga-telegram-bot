"""Admin in-bot panel for the subscription-freeze feature.

Buttons-driven editor for FreezeSettingsService (the singleton JSON-backed
store that controls enabled/max_days_per_year/min_subscription_age_days/
cooldown_days/min_freeze_days/max_single_freeze_days knobs).

Callback prefix: ``admin_freeze_``.
"""

from __future__ import annotations

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.freeze_settings_service import FreezeSettingsService
from app.states import FreezeAdminStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


# ---------------- Keyboards ----------------


def _config_keyboard(cfg: dict) -> InlineKeyboardMarkup:
    on = '🟢' if cfg.get('enabled') else '🔴'
    rows = [
        [InlineKeyboardButton(
            text=f'{on} Включено: {"Да" if cfg.get("enabled") else "Нет"}',
            callback_data='admin_freeze_toggle',
        )],
        [InlineKeyboardButton(
            text=f'📅 Макс. дней в год: {cfg.get("max_days_per_year", 30)} дн.',
            callback_data='admin_freeze_edit_maxyear',
        )],
        [InlineKeyboardButton(
            text=f'👤 Мин. возраст подписки: {cfg.get("min_subscription_age_days", 7)} дн.',
            callback_data='admin_freeze_edit_minage',
        )],
        [InlineKeyboardButton(
            text=f'⏳ Кулдаун между заморозками: {cfg.get("cooldown_days", 7)} дн.',
            callback_data='admin_freeze_edit_cooldown',
        )],
        [InlineKeyboardButton(
            text=f'❄️ Мин. длина заморозки: {cfg.get("min_freeze_days", 3)} дн.',
            callback_data='admin_freeze_edit_minfreeze',
        )],
        [InlineKeyboardButton(
            text=f'🔒 Макс. длина заморозки: {cfg.get("max_single_freeze_days", 30)} дн.',
            callback_data='admin_freeze_edit_maxsingle',
        )],
        [InlineKeyboardButton(text='⬅ В настройки', callback_data='admin_submenu_settings')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------- Render helper ----------------


def _format_config(cfg: dict) -> str:
    return (
        '❄️ <b>Настройки заморозки подписки</b>\n\n'
        f'Включено: <b>{"Да" if cfg.get("enabled") else "Нет"}</b>\n'
        f'Макс. дней заморозки в год: <b>{cfg.get("max_days_per_year", 30)} дн.</b>\n'
        f'Мин. возраст подписки: <b>{cfg.get("min_subscription_age_days", 7)} дн.</b>\n'
        f'Кулдаун между заморозками: <b>{cfg.get("cooldown_days", 7)} дн.</b>\n'
        f'Мин. длина одной заморозки: <b>{cfg.get("min_freeze_days", 3)} дн.</b>\n'
        f'Макс. длина одной заморозки: <b>{cfg.get("max_single_freeze_days", 30)} дн.</b>\n\n'
        f'<i>Заморозка позволяет пользователям ставить подписку на паузу, сохраняя оставшееся время.</i>'
    )


# ---------------- Top-level entry ----------------


@admin_required
@error_handler
async def open_panel(callback: types.CallbackQuery, **kwargs):
    cfg = FreezeSettingsService.get_config()
    await callback.message.edit_text(_format_config(cfg), parse_mode='HTML', reply_markup=_config_keyboard(cfg))
    await callback.answer()


# ---------------- Toggle enabled ----------------


@admin_required
@error_handler
async def toggle_enabled(callback: types.CallbackQuery, **kwargs):
    FreezeSettingsService.set_enabled(not FreezeSettingsService.is_enabled())
    cfg = FreezeSettingsService.get_config()
    await callback.message.edit_text(_format_config(cfg), parse_mode='HTML', reply_markup=_config_keyboard(cfg))
    await callback.answer('✅ Переключено')


# ---------------- Numeric edits via FSM ----------------


@admin_required
@error_handler
async def edit_maxyear(callback: types.CallbackQuery, state: FSMContext, **kwargs):
    await state.set_state(FreezeAdminStates.waiting_for_maxyear)
    await callback.message.answer(
        '📅 Введите максимальное количество дней заморозки в год (0–365):\n\nОтправьте число сообщением или /cancel.'
    )
    await callback.answer()


@admin_required
@error_handler
async def edit_minage(callback: types.CallbackQuery, state: FSMContext, **kwargs):
    await state.set_state(FreezeAdminStates.waiting_for_minage)
    await callback.message.answer(
        '👤 Введите минимальный возраст подписки в днях (0–365):\n\nОтправьте число сообщением или /cancel.'
    )
    await callback.answer()


@admin_required
@error_handler
async def edit_cooldown(callback: types.CallbackQuery, state: FSMContext, **kwargs):
    await state.set_state(FreezeAdminStates.waiting_for_cooldown)
    await callback.message.answer(
        '⏳ Введите кулдаун между заморозками в днях (0–365):\n\nОтправьте число сообщением или /cancel.'
    )
    await callback.answer()


@admin_required
@error_handler
async def edit_minfreeze(callback: types.CallbackQuery, state: FSMContext, **kwargs):
    await state.set_state(FreezeAdminStates.waiting_for_minfreeze)
    await callback.message.answer(
        '❄️ Введите минимальную длину одной заморозки в днях (1–365):\n\nОтправьте число сообщением или /cancel.'
    )
    await callback.answer()


@admin_required
@error_handler
async def edit_maxsingle(callback: types.CallbackQuery, state: FSMContext, **kwargs):
    await state.set_state(FreezeAdminStates.waiting_for_maxsingle)
    await callback.message.answer(
        '🔒 Введите максимальную длину одной заморозки в днях (1–365):\n\nОтправьте число сообщением или /cancel.'
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

    if current_state == FreezeAdminStates.waiting_for_maxyear:
        if value < 0 or value > 365:
            await message.answer('❌ Вне диапазона [0; 365]. Повторите или /cancel.')
            return
        result = FreezeSettingsService.set_max_days_per_year(value)
        if result is False:
            await message.answer('❌ Не удалось сохранить. Повторите или /cancel.')
            return

    elif current_state == FreezeAdminStates.waiting_for_minage:
        if value < 0 or value > 365:
            await message.answer('❌ Вне диапазона [0; 365]. Повторите или /cancel.')
            return
        result = FreezeSettingsService.set_min_subscription_age_days(value)
        if result is False:
            await message.answer('❌ Не удалось сохранить. Повторите или /cancel.')
            return

    elif current_state == FreezeAdminStates.waiting_for_cooldown:
        if value < 0 or value > 365:
            await message.answer('❌ Вне диапазона [0; 365]. Повторите или /cancel.')
            return
        result = FreezeSettingsService.set_cooldown_days(value)
        if result is False:
            await message.answer('❌ Не удалось сохранить. Повторите или /cancel.')
            return

    elif current_state == FreezeAdminStates.waiting_for_minfreeze:
        if value < 1 or value > 365:
            await message.answer('❌ Вне диапазона [1; 365]. Повторите или /cancel.')
            return
        result = FreezeSettingsService.set_min_freeze_days(value)
        if result is False:
            await message.answer('❌ Не удалось сохранить. Повторите или /cancel.')
            return

    elif current_state == FreezeAdminStates.waiting_for_maxsingle:
        if value < 1 or value > 365:
            await message.answer('❌ Вне диапазона [1; 365]. Повторите или /cancel.')
            return
        result = FreezeSettingsService.set_max_single_freeze_days(value)
        if result is False:
            await message.answer('❌ Не удалось сохранить. Повторите или /cancel.')
            return

    else:
        await state.clear()
        await message.answer('❌ Неизвестное состояние. Отменено.')
        return

    await state.clear()
    cfg = FreezeSettingsService.get_config()
    await message.answer(_format_config(cfg), parse_mode='HTML', reply_markup=_config_keyboard(cfg))


# ---------------- Registration ----------------


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(open_panel, F.data == 'admin_freeze_menu')
    dp.callback_query.register(toggle_enabled, F.data == 'admin_freeze_toggle')

    dp.callback_query.register(edit_maxyear, F.data == 'admin_freeze_edit_maxyear')
    dp.callback_query.register(edit_minage, F.data == 'admin_freeze_edit_minage')
    dp.callback_query.register(edit_cooldown, F.data == 'admin_freeze_edit_cooldown')
    dp.callback_query.register(edit_minfreeze, F.data == 'admin_freeze_edit_minfreeze')
    dp.callback_query.register(edit_maxsingle, F.data == 'admin_freeze_edit_maxsingle')

    # FSM message handlers — gated on state
    dp.message.register(_handle_numeric_input, FreezeAdminStates.waiting_for_maxyear)
    dp.message.register(_handle_numeric_input, FreezeAdminStates.waiting_for_minage)
    dp.message.register(_handle_numeric_input, FreezeAdminStates.waiting_for_cooldown)
    dp.message.register(_handle_numeric_input, FreezeAdminStates.waiting_for_minfreeze)
    dp.message.register(_handle_numeric_input, FreezeAdminStates.waiting_for_maxsingle)
