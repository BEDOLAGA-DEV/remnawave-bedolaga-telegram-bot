"""Телеграм-редактор: лимиты трафика по серверам тарифа (раньше — только кабинет)."""

import html
from typing import Any

from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.server_squad import get_all_server_squads
from app.database.crud.tariff import get_tariff_by_id, update_tariff
from app.database.models import Tariff, User
from app.localization.texts import get_texts
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler


def _limits(tariff: Tariff) -> dict[str, Any]:
    return dict(getattr(tariff, 'server_traffic_limits', None) or {})


def limit_for(tariff: Tariff, squad_uuid: str) -> int:
    """Лимит сервера в ГБ; 0 = общий лимит тарифа. Терпит старую форму (число вместо объекта)."""
    raw = _limits(tariff).get(squad_uuid)
    if isinstance(raw, dict):
        raw = raw.get('traffic_limit_gb')
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def format_server_limits_summary(tariff: Tariff) -> str:
    """Строка для карточки тарифа."""
    count = sum(1 for uuid in _limits(tariff) if limit_for(tariff, uuid) > 0)
    return f'{count} с особым лимитом' if count else 'по тарифу'


def _visible_squads(tariff: Tariff, squads: list) -> list:
    allowed = set(getattr(tariff, 'allowed_squads', None) or [])
    return [s for s in squads if not allowed or s.squad_uuid in allowed]


def render_server_limits(tariff: Tariff) -> str:
    return (
        f'🗄️ <b>Лимиты трафика по серверам</b>\n\n'
        f'Тариф: <b>{html.escape(tariff.name)}</b>\n'
        f'Общий лимит тарифа: <b>{tariff.traffic_limit_gb or "∞"} ГБ</b>\n\n'
        'Нажмите на сервер и введите лимит в ГБ. <code>0</code> — использовать общий лимит тарифа.'
    )


def get_server_limits_keyboard(tariff: Tariff, squads: list, language: str) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    buttons = []
    for squad in _visible_squads(tariff, squads):
        limit = limit_for(tariff, squad.squad_uuid)
        label = f'{squad.display_name}: {limit} ГБ' if limit else f'{squad.display_name}: по тарифу'
        buttons.append(
            [InlineKeyboardButton(text=label, callback_data=f'admin_tariff_srv_limit:{tariff.id}:{squad.squad_uuid}')]
        )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff.id}')])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _render_screen(
    target: types.Message, tariff: Tariff, db: AsyncSession, language: str, *, edit: bool, prefix: str = ''
) -> None:
    squads, _ = await get_all_server_squads(db, limit=10000)
    text = f'{prefix}{render_server_limits(tariff)}'
    keyboard = get_server_limits_keyboard(tariff, squads, language)
    if edit:
        await target.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    else:
        await target.answer(text, reply_markup=keyboard, parse_mode='HTML')


@admin_required
@error_handler
async def show_server_limits(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.clear()
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return
    await _render_screen(callback.message, tariff, db, db_user.language, edit=True)
    await callback.answer()


@admin_required
@error_handler
async def start_edit_server_limit(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    _, tariff_id, squad_uuid = callback.data.split(':', 2)
    tariff = await get_tariff_by_id(db, int(tariff_id))
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return
    await state.set_state(AdminStates.editing_tariff_server_limit)
    await state.update_data(tariff_id=tariff.id, squad_uuid=squad_uuid, language=db_user.language)
    texts = get_texts(db_user.language)
    current = limit_for(tariff, squad_uuid)
    await callback.message.edit_text(
        f'🗄️ <b>Лимит сервера</b>\n\nТариф: <b>{html.escape(tariff.name)}</b>\n'
        f'Текущее значение: <b>{current} ГБ</b>{"" if current else " (по тарифу)"}\n\n'
        'Введите лимит в ГБ целым числом. <code>0</code> — по тарифу.',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_edit_server_limits:{tariff.id}')]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_server_limit_input(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    data = await state.get_data()
    tariff = await get_tariff_by_id(db, data.get('tariff_id')) if data.get('tariff_id') is not None else None
    squad_uuid = data.get('squad_uuid')
    if tariff is None or not squad_uuid:
        await message.answer('Тариф не найден')
        await state.clear()
        return

    try:
        limit = int((message.text or '').strip())
        if limit < 0:
            raise ValueError
    except ValueError:
        await message.answer(
            '❌ Введите целое число гигабайт, не меньше нуля. <code>0</code> — по тарифу.', parse_mode='HTML'
        )
        return

    # Новый словарь, а не правка хранимого: JSON-колонка иначе не увидит изменения.
    limits = {uuid: value for uuid, value in _limits(tariff).items() if uuid != squad_uuid}
    if limit > 0:
        limits[squad_uuid] = {'traffic_limit_gb': limit}
    tariff = await update_tariff(db, tariff, server_traffic_limits=limits)
    await state.clear()
    confirmation = f'✅ Лимит установлен: {limit} ГБ' if limit else '✅ Лимит снят — по тарифу'
    await _render_screen(message, tariff, db, db_user.language, edit=False, prefix=f'{confirmation}\n\n')


def register_server_limits_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_server_limits, F.data.startswith('admin_tariff_edit_server_limits:'))
    dp.callback_query.register(start_edit_server_limit, F.data.startswith('admin_tariff_srv_limit:'))
    dp.message.register(process_server_limit_input, AdminStates.editing_tariff_server_limit)
