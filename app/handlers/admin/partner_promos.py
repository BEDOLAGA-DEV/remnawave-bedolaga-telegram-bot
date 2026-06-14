"""Admin management for partner promos (the "Партнёрские офферы" showcase).

CRUD over app.database.crud.partner_promo, mirroring the FAQ admin handler. The
public showcase itself is gated by settings.PARTNER_SHOWCASE_ENABLED (env-only);
this admin section always works so promos can be prepared before enabling it.

title/description are JSONB {lang: text} dicts. The bot edits the entry for the
admin's own language (db_user.language), merging into any existing translations.
"""

import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import partner_promo as crud
from app.database.models import User
from app.localization.texts import get_texts
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


def _pick(value: dict | None, lang: str) -> str:
    """Pick the display text from a {lang: text} dict (mirrors menu.py)."""
    data = value or {}
    return data.get(lang) or data.get('ru') or next(iter(data.values()), '') or ''


def _parse_id(data: str | None) -> int | None:
    try:
        return int((data or '').split(':')[1])
    except (ValueError, IndexError):
        return None


def _cancel_kb() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ Отмена', callback_data='admin_partner_promos_cancel')]]
    )


def _back_to_list_kb() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ К офферам', callback_data='admin_partner_promos')]]
    )


def _back_to_item_kb(promo_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='⬅️ К офферу', callback_data=f'admin_partner_promos_item:{promo_id}')]
        ]
    )


async def _build_overview(db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    promos = await crud.list_all(db)

    header = '🤝 <b>Партнёрские офферы</b>'
    description = 'Блок «Специальные предложения от наших партнёров» в кабинете и меню бота.'
    if settings.PARTNER_SHOWCASE_ENABLED:
        status = '✅ Показ блока включён.'
    else:
        status = (
            '⚠️ Показ блока выключен. Пользователи его НЕ видят. Включите '
            '<code>PARTNER_SHOWCASE_ENABLED=true</code> в .env и перезапустите бота.'
        )

    if promos:
        rows = []
        for index, promo in enumerate(promos, start=1):
            title = _pick(promo.title, db_user.language).strip() or 'Без названия'
            if len(title) > 50:
                title = f'{title[:47]}...'
            badge = '✅' if promo.is_active else '🚫'
            rows.append(f'{index}. {badge} {html.escape(title)} — кликов: {promo.click_count}')
        listing = '<b>Офферы:</b>\n' + '\n'.join(rows)
    else:
        listing = 'Офферов пока нет.'

    text = '\n\n'.join([header, description, status, listing, 'Выберите действие:'])

    buttons: list[list[types.InlineKeyboardButton]] = [
        [types.InlineKeyboardButton(text='➕ Добавить оффер', callback_data='admin_partner_promos_create')]
    ]
    for promo in promos[:25]:
        title = _pick(promo.title, db_user.language).strip() or 'Без названия'
        if len(title) > 40:
            title = f'{title[:37]}...'
        badge = '✅' if promo.is_active else '🚫'
        buttons.append(
            [types.InlineKeyboardButton(text=f'{badge} {title}', callback_data=f'admin_partner_promos_item:{promo.id}')]
        )
    buttons.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_communications')])

    return text, types.InlineKeyboardMarkup(inline_keyboard=buttons)


@admin_required
@error_handler
async def show_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    text, markup = await _build_overview(db_user, db)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@admin_required
@error_handler
async def show_item(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    promo_id = _parse_id(callback.data)
    promo = await crud.get(db, promo_id) if promo_id else None
    if not promo:
        await callback.answer('Оффер не найден.', show_alert=True)
        return

    texts = get_texts(db_user.language)
    title = _pick(promo.title, db_user.language) or '—'
    descr = _pick(promo.description, db_user.language) or '—'
    badge = '✅ активен' if promo.is_active else '🚫 выключен'
    lines = [
        f'🤝 <b>Оффер #{promo.id}</b>',
        f'Заголовок: {html.escape(title)}',
        f'Описание: {html.escape(descr)}',
        f'Ссылка: {html.escape(promo.url)}',
        f'Картинка: {html.escape(promo.image_url) if promo.image_url else "—"}',
        f'Статус: {badge}',
        f'Порядок: {promo.sort_order} · кликов: {promo.click_count}',
    ]
    toggle_label = '🚫 Выключить' if promo.is_active else '✅ Включить'
    buttons = [
        [
            types.InlineKeyboardButton(text='✏️ Заголовок', callback_data=f'admin_partner_promos_edit_title:{promo.id}'),
            types.InlineKeyboardButton(text='✏️ Описание', callback_data=f'admin_partner_promos_edit_desc:{promo.id}'),
        ],
        [
            types.InlineKeyboardButton(text='🔗 Ссылка', callback_data=f'admin_partner_promos_edit_url:{promo.id}'),
            types.InlineKeyboardButton(text='🖼 Картинка', callback_data=f'admin_partner_promos_edit_image:{promo.id}'),
        ],
        [types.InlineKeyboardButton(text=toggle_label, callback_data=f'admin_partner_promos_toggle:{promo.id}')],
        [
            types.InlineKeyboardButton(text='⬆️', callback_data=f'admin_partner_promos_move:{promo.id}:up'),
            types.InlineKeyboardButton(text='⬇️', callback_data=f'admin_partner_promos_move:{promo.id}:down'),
            types.InlineKeyboardButton(text='🗑 Удалить', callback_data=f'admin_partner_promos_delete:{promo.id}'),
        ],
        [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_partner_promos')],
    ]
    await callback.message.edit_text('\n'.join(lines), reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


# --------------------------------------------------------------------------
# Create flow: title -> url. Description/image are added later via edit.
# --------------------------------------------------------------------------
@admin_required
@error_handler
async def start_create(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    await state.set_state(AdminStates.creating_partner_promo_title)
    await state.update_data(pp_lang=db_user.language)
    await callback.message.edit_text('Введите заголовок оффера:', reply_markup=_cancel_kb())
    await callback.answer()


@admin_required
@error_handler
async def cancel(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    await state.clear()
    await show_list(callback, db_user, db)


@admin_required
@error_handler
async def process_new_title(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    title = (message.text or '').strip()
    if not title:
        await message.answer('❌ Заголовок не может быть пустым. Повторите:')
        return
    if len(title) > 255:
        await message.answer('❌ Заголовок слишком длинный (макс. 255). Повторите:')
        return
    await state.update_data(pp_title=title)
    await state.set_state(AdminStates.creating_partner_promo_url)
    await message.answer('Отправьте ссылку оффера (https://...):', reply_markup=_cancel_kb())


@admin_required
@error_handler
async def process_new_url(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    url = (message.text or '').strip()
    data = await state.get_data()
    lang = data.get('pp_lang', db_user.language)
    title = data.get('pp_title', '')
    try:
        promos = await crud.list_all(db)
        next_order = max((p.sort_order for p in promos), default=-1) + 1
        promo = await crud.create(db, title={lang: title}, url=url, sort_order=next_order)
    except ValueError:
        await message.answer('❌ Ссылка должна быть валидным https:// URL. Повторите:')
        return
    await state.clear()
    logger.info('Admin created partner promo', telegram_id=db_user.telegram_id, promo_id=promo.id)
    await message.answer(
        '✅ Оффер создан. Добавьте описание/картинку в карточке оффера.',
        reply_markup=_back_to_item_kb(promo.id),
    )


# --------------------------------------------------------------------------
# Edit flows.
# --------------------------------------------------------------------------
async def _start_edit(callback: types.CallbackQuery, state: FSMContext, db_user: User, target_state, prompt: str):
    promo_id = _parse_id(callback.data)
    if not promo_id:
        await callback.answer()
        return
    await state.set_state(target_state)
    await state.update_data(pp_edit_id=promo_id, pp_lang=db_user.language)
    await callback.message.edit_text(prompt, reply_markup=_cancel_kb())
    await callback.answer()


@admin_required
@error_handler
async def start_edit_title(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    await _start_edit(callback, state, db_user, AdminStates.editing_partner_promo_title, 'Введите новый заголовок:')


@admin_required
@error_handler
async def start_edit_url(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    await _start_edit(callback, state, db_user, AdminStates.editing_partner_promo_url, 'Введите новую ссылку (https://...):')


@admin_required
@error_handler
async def start_edit_desc(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    await _start_edit(callback, state, db_user, AdminStates.editing_partner_promo_description, 'Введите новое описание:')


@admin_required
@error_handler
async def start_edit_image(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    await _start_edit(
        callback,
        state,
        db_user,
        AdminStates.editing_partner_promo_image,
        'Отправьте https:// ссылку на картинку, или «-» чтобы убрать:',
    )


async def _resolve_edit(state: FSMContext, db: AsyncSession, message: types.Message):
    data = await state.get_data()
    promo_id = data.get('pp_edit_id')
    promo = await crud.get(db, promo_id) if promo_id else None
    if not promo:
        await state.clear()
        await message.answer('Оффер не найден.', reply_markup=_back_to_list_kb())
        return None, None, None
    return promo_id, promo, data.get('pp_lang', 'ru')


@admin_required
@error_handler
async def process_edit_title(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    text = (message.text or '').strip()
    if not text:
        await message.answer('❌ Пусто. Повторите:')
        return
    promo_id, promo, lang = await _resolve_edit(state, db, message)
    if promo is None:
        return
    await crud.update_promo(db, promo_id, title={**(promo.title or {}), lang: text})
    await state.clear()
    await message.answer('✅ Заголовок обновлён.', reply_markup=_back_to_item_kb(promo_id))


@admin_required
@error_handler
async def process_edit_desc(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    text = (message.text or '').strip()
    promo_id, promo, lang = await _resolve_edit(state, db, message)
    if promo is None:
        return
    await crud.update_promo(db, promo_id, description={**(promo.description or {}), lang: text})
    await state.clear()
    await message.answer('✅ Описание обновлено.', reply_markup=_back_to_item_kb(promo_id))


@admin_required
@error_handler
async def process_edit_url(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    url = (message.text or '').strip()
    data = await state.get_data()
    promo_id = data.get('pp_edit_id')
    if not promo_id:
        await state.clear()
        await message.answer('Оффер не найден.', reply_markup=_back_to_list_kb())
        return
    try:
        updated = await crud.update_promo(db, promo_id, url=url)
    except ValueError:
        await message.answer('❌ Ссылка должна быть валидным https:// URL. Повторите:')
        return
    if updated is None:
        await state.clear()
        await message.answer('Оффер не найден.', reply_markup=_back_to_list_kb())
        return
    await state.clear()
    await message.answer('✅ Ссылка обновлена.', reply_markup=_back_to_item_kb(promo_id))


@admin_required
@error_handler
async def process_edit_image(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    raw = (message.text or '').strip()
    image_url = None if raw in ('', '-', '—') else raw
    data = await state.get_data()
    promo_id = data.get('pp_edit_id')
    if not promo_id:
        await state.clear()
        await message.answer('Оффер не найден.', reply_markup=_back_to_list_kb())
        return
    try:
        updated = await crud.update_promo(db, promo_id, image_url=image_url)
    except ValueError:
        await message.answer('❌ Картинка должна быть https:// URL (или «-» чтобы убрать). Повторите:')
        return
    if updated is None:
        await state.clear()
        await message.answer('Оффер не найден.', reply_markup=_back_to_list_kb())
        return
    await state.clear()
    await message.answer('✅ Картинка обновлена.', reply_markup=_back_to_item_kb(promo_id))


# --------------------------------------------------------------------------
# Toggle / delete / reorder.
# --------------------------------------------------------------------------
@admin_required
@error_handler
async def toggle(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    promo_id = _parse_id(callback.data)
    promo = await crud.get(db, promo_id) if promo_id else None
    if not promo:
        await callback.answer('Оффер не найден.', show_alert=True)
        return
    await crud.update_promo(db, promo_id, is_active=not promo.is_active)
    await callback.answer('✅ Включён.' if not promo.is_active else '🚫 Выключен.', show_alert=True)
    await show_item(callback, db_user, db)


@admin_required
@error_handler
async def delete(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    promo_id = _parse_id(callback.data)
    if promo_id:
        await crud.delete(db, promo_id)
    await callback.answer('🗑️ Оффер удалён.', show_alert=True)
    await show_list(callback, db_user, db)


@admin_required
@error_handler
async def move(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    parts = (callback.data or '').split(':')
    try:
        promo_id = int(parts[1])
        direction = parts[2]
    except (ValueError, IndexError):
        await callback.answer()
        return

    promos = await crud.list_all(db)  # ordered by sort_order asc, id asc
    index = next((i for i, promo in enumerate(promos) if promo.id == promo_id), None)
    if index is None:
        await callback.answer()
        return
    swap = index - 1 if direction == 'up' else index + 1
    if swap < 0 or swap >= len(promos):
        await callback.answer()
        return

    ordered = list(promos)
    ordered[index], ordered[swap] = ordered[swap], ordered[index]
    # Reassign sort_order = position for the whole list so order is always
    # well-defined even if some legacy rows shared the same sort_order.
    for position, promo in enumerate(ordered):
        if promo.sort_order != position:
            await crud.update_promo(db, promo.id, sort_order=position)

    await callback.answer('✅ Порядок обновлён.', show_alert=True)
    await show_item(callback, db_user, db)


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_list, F.data == 'admin_partner_promos')
    dp.callback_query.register(start_create, F.data == 'admin_partner_promos_create')
    dp.callback_query.register(cancel, F.data == 'admin_partner_promos_cancel')
    dp.callback_query.register(show_item, F.data.startswith('admin_partner_promos_item:'))
    dp.callback_query.register(start_edit_title, F.data.startswith('admin_partner_promos_edit_title:'))
    dp.callback_query.register(start_edit_url, F.data.startswith('admin_partner_promos_edit_url:'))
    dp.callback_query.register(start_edit_desc, F.data.startswith('admin_partner_promos_edit_desc:'))
    dp.callback_query.register(start_edit_image, F.data.startswith('admin_partner_promos_edit_image:'))
    dp.callback_query.register(toggle, F.data.startswith('admin_partner_promos_toggle:'))
    dp.callback_query.register(delete, F.data.startswith('admin_partner_promos_delete:'))
    dp.callback_query.register(move, F.data.startswith('admin_partner_promos_move:'))

    dp.message.register(process_new_title, AdminStates.creating_partner_promo_title)
    dp.message.register(process_new_url, AdminStates.creating_partner_promo_url)
    dp.message.register(process_edit_title, AdminStates.editing_partner_promo_title)
    dp.message.register(process_edit_url, AdminStates.editing_partner_promo_url)
    dp.message.register(process_edit_desc, AdminStates.editing_partner_promo_description)
    dp.message.register(process_edit_image, AdminStates.editing_partner_promo_image)
